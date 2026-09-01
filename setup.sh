#!/usr/bin/env bash
#
# Разворачивает SRv6-лабу на FRR и проверяет, что IPv4 ходит через SRv6-туннель.
# Всё, что нужно, лежит рядом: топология и конфиги FRR.
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

info "Жду сходимости IS-IS"
for _ in $(seq 30); do
  if sudo docker exec $LAB-r1 ip -6 route show 2>/dev/null | grep -q '^fc00:3::/64.*eth1'; then
    break
  fi
  sleep 2
done

# FRR читает frr.conf при старте контейнера, когда IS-IS ещё не сошёлся,
# и не может разрешить SID как next-hop — маршрут уходит в менеджмент-интерфейс.
# Переустанавливаем политики после сходимости.
info "Переустанавливаю SRv6-политики"
sudo docker exec $LAB-r1 vtysh -c "configure terminal" \
  -c "no ip route 10.0.3.1/32 fc00:3:0:0:100:: vrf vrf1 nexthop-vrf default segments fc00:3:0:0:100::" >/dev/null
sudo docker exec $LAB-r3 vtysh -c "configure terminal" \
  -c "no ip route 10.0.1.1/32 fc00:1:0:0:100:: vrf vrf1 nexthop-vrf default segments fc00:1:0:0:100::" >/dev/null
sleep 1
sudo docker exec $LAB-r1 vtysh -c "configure terminal" \
  -c "ip route 10.0.3.1/32 fc00:3:0:0:100:: vrf vrf1 nexthop-vrf default segments fc00:3:0:0:100::" >/dev/null
sudo docker exec $LAB-r3 vtysh -c "configure terminal" \
  -c "ip route 10.0.1.1/32 fc00:1:0:0:100:: vrf vrf1 nexthop-vrf default segments fc00:1:0:0:100::" >/dev/null
sleep 2

# --- проверка ----------------------------------------------------------------

info "Проверяю SID на r3"
sudo docker exec $LAB-r3 ip -6 route show | grep seg6local || true

info "Проверяю SRv6-политику на r1"
sudo docker exec $LAB-r1 ip route show vrf vrf1 || true

info "Пингую 10.0.3.1 с r1 через SRv6"
if sudo docker exec $LAB-r1 ip vrf exec vrf1 ping -c4 10.0.3.1; then
  echo
  grn "Готово. IPv4 ходит внутри SRv6."
  echo
  echo "Посмотреть инкапсуляцию на транзитном узле:"
  echo "  PID=\$(sudo docker inspect -f '{{.State.Pid}}' $LAB-r2)"
  echo "  sudo nsenter -t \$PID -n tcpdump -ni eth1 -v 'ip6 proto 43'"
  echo
  echo "Погасить лабу:"
  echo "  sudo containerlab destroy -t $TOPO"
else
  echo
  red "Пинг не прошёл. Что посмотреть:"
  echo "  sudo docker exec $LAB-r1 vtysh -c 'show isis neighbor'"
  echo "  sudo docker exec $LAB-r3 ip -6 route show | grep seg6local"
  echo "  sudo docker logs $LAB-r1 2>&1 | tail -20"
  exit 1
fi
