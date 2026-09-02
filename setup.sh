#!/usr/bin/env bash
#
# Разворачивает SRv6-лабу на FRR: IS-IS в underlay, BGP L3VPN поверх него.
# Две независимые VRF — RED и BLUE. Статических маршрутов и статических SID нет.
#
set -euo pipefail

TOPO="srv6-lab.clab.yml"
LAB="clab-srv6-lab"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\033[1m==> %s\033[0m\n' "$*"; }

cd "$(dirname "$0")"

# --- проверки окружения ------------------------------------------------------

info "Проверяю окружение"

command -v docker >/dev/null || { red "docker не установлен"; exit 1; }
command -v containerlab >/dev/null || {
  red "containerlab не установлен"
  echo '   bash -c "$(curl -sL https://get.containerlab.dev)"'
  exit 1
}

if ! sudo docker info >/dev/null 2>&1; then
  red "демон docker не отвечает"
  echo "   sudo service docker start"
  exit 1
fi

sudo modprobe vrf dummy 2>/dev/null || true

sudo ip -6 route del fc00:9::1/128 2>/dev/null || true
if ! sudo ip -6 route add fc00:9::1/128 encap seg6local action End dev lo 2>/dev/null; then
  red "ядро не умеет seg6local — SRv6 работать не будет"
  echo "   нужны CONFIG_LWTUNNEL и CONFIG_IPV6_SEG6_LWTUNNEL"
  echo "   в WSL2 штатное ядро собрано без них, см. README"
  exit 1
fi
sudo ip -6 route del fc00:9::1/128

grn "    ядро поддерживает SRv6, docker работает"

# --- запуск ------------------------------------------------------------------

info "Поднимаю лабу (первый запуск скачает образ FRR)"
sudo containerlab deploy -t "$TOPO"

# Ждём, пока IS-IS разнесёт локаторы: без этого BGP не разрешит next-hop.
info "Жду сходимости IS-IS"
for _ in $(seq 30); do
  if sudo docker exec $LAB-r1 ip -6 route show 2>/dev/null | grep -q '^fc00:3::/64.*eth1'; then
    grn "    r1 видит локатор r3"
    break
  fi
  sleep 2
done

# Сессия iBGP поднимается по loopback'ам, маршруты VPNv4 приезжают в VRF
# с SRv6-инкапсуляцией. Никакой переустановки политик руками, как со статикой:
# BGP сам переоценит next-hop, когда IS-IS сойдётся.
info "Жду маршруты BGP L3VPN в VRF"
ok=0
for _ in $(seq 45); do
  if sudo docker exec $LAB-r1 ip route show vrf RED  2>/dev/null | grep -q '10.0.3.1' &&
     sudo docker exec $LAB-r1 ip route show vrf BLUE 2>/dev/null | grep -q '10.1.3.1' &&
     sudo docker exec $LAB-r3 ip route show vrf RED  2>/dev/null | grep -q '10.0.1.1' &&
     sudo docker exec $LAB-r3 ip route show vrf BLUE 2>/dev/null | grep -q '10.1.1.1'; then
    ok=1
    break
  fi
  sleep 2
done

if [ "$ok" -ne 1 ]; then
  red "маршруты VPN не доехали"
  echo "  sudo docker exec $LAB-r1 vtysh -c 'show bgp ipv4 vpn summary'"
  echo "  sudo docker exec $LAB-r1 vtysh -c 'show bgp ipv4 vpn'"
  exit 1
fi

# --- проверка ----------------------------------------------------------------

info "SID, выданные BGP на r1 (uDT4 на каждую VRF)"
sudo docker exec $LAB-r1 vtysh -c "show segment-routing srv6 sid" || true

info "SID в таблице ядра на r3"
sudo docker exec $LAB-r3 ip -6 route show | grep seg6local || true

info "Маршруты в VRF RED на r1"
sudo docker exec $LAB-r1 ip route show vrf RED || true

info "Маршруты в VRF BLUE на r1"
sudo docker exec $LAB-r1 ip route show vrf BLUE || true

hints() {
  red "Пинг в VRF $1 не прошёл. Что посмотреть:"
  echo "  sudo docker exec $LAB-r1 vtysh -c 'show isis neighbor'"
  echo "  sudo docker exec $LAB-r1 vtysh -c 'show bgp ipv4 vpn summary'"
  echo "  sudo docker exec $LAB-r1 vtysh -c 'show bgp vrf $1 ipv4 unicast'"
  echo "  sudo docker exec $LAB-r3 ip -6 route show | grep seg6local"
  echo "  sudo docker logs $LAB-r1 2>&1 | tail -20"
  exit 1
}

info "Пингую 10.0.3.1 из RED"
sudo docker exec $LAB-r1 ip vrf exec RED ping -c3 -I 10.0.1.1 10.0.3.1 || hints RED

info "Пингую 10.1.3.1 из BLUE"
sudo docker exec $LAB-r1 ip vrf exec BLUE ping -c3 -I 10.1.1.1 10.1.3.1 || hints BLUE

# VRF изолированы: адрес чужой VRF не должен быть виден.
info "Проверяю изоляцию: из RED адрес BLUE должен быть недоступен"
if sudo docker exec $LAB-r1 ip vrf exec RED ping -c1 -W2 -I 10.0.1.1 10.1.3.1 >/dev/null 2>&1; then
  red "трафик утёк между VRF — это ошибка конфигурации"
  exit 1
fi
grn "    из RED в BLUE не проходит, как и должно быть"

info "Проверяю изоляцию: из BLUE адрес RED должен быть недоступен"
if sudo docker exec $LAB-r1 ip vrf exec BLUE ping -c1 -W2 -I 10.1.1.1 10.0.3.1 >/dev/null 2>&1; then
  red "трафик утёк между VRF — это ошибка конфигурации"
  exit 1
fi
grn "    из BLUE в RED не проходит, как и должно быть"

echo
grn "Готово. Две VRF ходят через SRv6, между собой не смешиваются."
echo
echo "Посмотреть инкапсуляцию на транзитном узле:"
echo "  PID=\$(sudo docker inspect -f '{{.State.Pid}}' $LAB-r2)"
echo "  sudo nsenter -t \$PID -n tcpdump -ni eth1 -v 'ip6 proto 43 or ip6 proto 4'"
echo
echo "Погасить лабу:"
echo "  sudo containerlab destroy -t $TOPO"
