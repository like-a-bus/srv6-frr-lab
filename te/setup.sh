#!/usr/bin/env bash
#
# srv6-lab2: 2 CE, 4 PE, 4 P.
# IS-IS в underlay на всех восьми узлах ядра, BGP L3VPN поверх,
# два route reflector'а на P1 и P3, CE подключены к двум PE каждый.
# Статических маршрутов нет.
#
set -euo pipefail

TOPO="srv6-lab2.clab.yml"
LAB="clab-srv6-lab2"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
info() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ex()   { sudo docker exec "$LAB-$1" "${@:2}"; }

cd "$(dirname "$0")"

info "Проверяю окружение"
command -v docker >/dev/null || { red "docker не установлен"; exit 1; }
command -v containerlab >/dev/null || {
  red "containerlab не установлен"
  echo '   bash -c "$(curl -sL https://get.containerlab.dev)"'; exit 1; }
sudo docker info >/dev/null 2>&1 || { red "демон docker не отвечает"; exit 1; }

sudo modprobe vrf dummy 2>/dev/null || true
sudo ip -6 route del fc00:9::1/128 2>/dev/null || true
if ! sudo ip -6 route add fc00:9::1/128 encap seg6local action End dev lo 2>/dev/null; then
  red "ядро не умеет seg6local — нужны CONFIG_LWTUNNEL и CONFIG_IPV6_SEG6_LWTUNNEL"
  exit 1
fi
sudo ip -6 route del fc00:9::1/128
grn "    ядро поддерживает SRv6"

info "Поднимаю лабу — 10 узлов, это дольше, чем в первой"
sudo containerlab deploy -t "$TOPO"

info "Жду сходимости IS-IS"
for _ in $(seq 45); do
  n=$(ex pe1 vtysh -c "show isis route" 2>/dev/null | grep -c 'fc00:1[2-4]::/64' || true)
  [ "${n:-0}" -ge 3 ] && { grn "    pe1 видит локаторы остальных PE"; break; }
  sleep 2
done

# isisd заполняет TE-адрес интерфейса только по событию "адрес добавлен".
# Если адрес появился на интерфейсе раньше, чем включился mpls-te, в LSP нет
# Local Interface IPv6 Address, и TED не может собрать ребро: у него нет ключа.
# Какие адреса успевают, а какие нет, решает гонка при старте, по интерфейсам.
# Снимаем и возвращаем адреса линков, чтобы событие гарантированно дошло.
# Снятие и возврат — два разных вызова vtysh: в одном транзакция схлопнется
# в ноль. Соседства IS-IS держатся на link-local, BGP ходит по loopback'ам.
info "Переустанавливаю адреса линков, чтобы TED собрала рёбра"
for n in p1 p2 p3 p4 pe1 pe2 pe3 pe4; do
  L=$(ex $n vtysh -c "show running-config" \
      | awk '/^interface /{i=$2; a=""} /^ ipv6 address /{a=$3} /^ ipv6 router isis/{if (i != "lo" && a != "") print i" "a}' || true)
  [ -n "$L" ] || continue
  D=(-c "conf t"); U=(-c "conf t")
  while read -r i a; do
    D+=(-c "interface $i" -c "no ipv6 address $a" -c "exit")
    U+=(-c "interface $i" -c "ipv6 address $a" -c "exit")
  done <<< "$L"
  ex $n vtysh "${D[@]}" -c "end" >/dev/null
  sleep 2
  ex $n vtysh "${U[@]}" -c "end" >/dev/null
done

# 12 линков в ядре, в TED каждое направление — отдельное ребро.
# isisd придерживает повторную генерацию LSP после серии изменений,
# поэтому рёбра появляются не сразу — ждём с запасом.
info "Жду рёбер в TED"
e=0; t0=$SECONDS
for _ in $(seq 45); do
  e=$(ex p1 vtysh -c "show isis mpls-te database detail" 2>/dev/null \
      | sed -nE 's/.*Vertices, ([0-9]+) Edges.*/\1/p' || true)
  [ "${e:-0}" -ge 24 ] && { grn "    24 ребра за $((SECONDS - t0)) с, топология для BGP-LS полная"; break; }
  sleep 2
done
[ "${e:-0}" -ge 24 ] || red "    рёбер в TED: ${e:-0} из 24, BGP-LS отдаст неполный граф"

# bgpd забирает у isisd полную топологию только в момент активации link-state.
# При старте контейнера это происходит раньше, чем isisd готов отвечать: запрос
# теряется, и таблица BGP-LS остаётся пустой навсегда. mpls-te export повторно
# базу не шлёт. Переактивация соседа ctrl после сборки TED повторяет запрос.
# Снятие и активация — два разных вызова vtysh, иначе транзакция схлопнется.
info "Переактивирую BGP-LS на рефлекторах, чтобы bgpd забрал топологию"
for x in "p1 d" "p3 e"; do
  set -- $x
  ex $1 vtysh -c "conf t" -c "router bgp 65000" -c "address-family link-state link-state" \
    -c "no neighbor fc00:0:$2::2 activate" -c "end" >/dev/null
  sleep 2
  ex $1 vtysh -c "conf t" -c "router bgp 65000" -c "address-family link-state link-state" \
    -c "neighbor fc00:0:$2::2 activate" -c "end" >/dev/null
done

# 8 узлов + 24 линка + 40 префиксов + 8 SRv6 SID = 80 NLRI от каждого рефлектора.
info "Жду топологию на ctrl"
ls=0
for _ in $(seq 30); do
  ls=$(ex ctrl vtysh -c "show bgp summary" 2>/dev/null \
    | awk '/Link-State Link-State Summary/{f=1} f && /^fc00:0:/ && $10 ~ /^[0-9]+$/ {s+=$10} END {print s+0}' || true)
  [ "${ls:-0}" -ge 160 ] && { grn "    ctrl получил по 80 NLRI от p1 и p3"; break; }
  sleep 2
done
[ "${ls:-0}" -ge 160 ] || red "    NLRI BGP-LS на ctrl: ${ls:-0} из 160, контроллер увидит неполную топологию"

info "Жду сессии BGP до рефлекторов"
for _ in $(seq 45); do
  up=0
  for pe in pe1 pe2 pe3 pe4; do
    c=$(ex $pe vtysh -c "show bgp ipv4 vpn summary" 2>/dev/null \
        | grep -cE '^fc00::(1|3) ' || true)
    up=$((up + ${c:-0}))
  done
  [ "$up" -ge 8 ] && { grn "    все 8 сессий на месте"; break; }
  sleep 2
done

# FRR не всегда экспортирует в VPN префиксы, выученные по eBGP от CE, если
# сессия поднялась позже разбора конфига (FRR #10623). Пересбор сессии с CE
# после схождения даёт нужный триггер.
info "Пересобираю сессии с CE, чтобы экспорт в VPN отработал наверняка"
for pe in pe1 pe2 pe3 pe4; do
  ex $pe vtysh -c "clear bgp vrf RED ipv4 unicast *" >/dev/null 2>&1 || true
done

info "Жду маршруты CE в VRF на дальней стороне"
ok=0
for _ in $(seq 45); do
  if ex pe1 ip route show vrf RED 2>/dev/null | grep -q '10.2.0.1' &&
     ex pe3 ip route show vrf RED 2>/dev/null | grep -q '10.1.0.1'; then
    ok=1; break
  fi
  sleep 2
done
[ "$ok" -eq 1 ] || { red "VPN-маршруты не доехали"
  echo "  sudo docker exec $LAB-pe1 vtysh -c 'show bgp vrf RED ipv4 unicast'"
  echo "  sudo docker exec $LAB-pe1 vtysh -c 'show bgp ipv4 vpn 10.1.0.1/32'"
  echo "  sudo docker exec $LAB-p1  vtysh -c 'show bgp ipv4 vpn'"; exit 1; }

info "Рефлектор P1: маршруты VPNv4, при том что ни одной VRF у него нет"
ex p1 vtysh -c "show bgp ipv4 vpn" || true

info "SRv6 в ядре у рефлектора — только от IS-IS, ни одной записи от bgp"
ex p1 ip -6 route show | grep seg6local || true

info "SID на PE3 — End.DT4 для RED"
ex pe3 vtysh -c "show segment-routing srv6 sid" || true

info "Маршрут CE2 в VRF RED на PE1: две записи, dual-homing active/active"
ex pe1 ip route show vrf RED || true

info "Тот же префикс глазами BGP — два пути с разными RD"
ex pe1 vtysh -c "show bgp vrf RED ipv4 unicast 10.2.0.1/32" || true

info "CE1 -> CE2"
ex ce1 ping -c3 -I 10.1.0.1 10.2.0.1

info "CE1 видит префикс через оба PE"
ex ce1 vtysh -c "show ip route 10.2.0.1/32" || true

echo
grn "Готово. 10 узлов, 8 iBGP к рефлекторам, 4 eBGP до CE, трафик через SRv6."
echo
echo "Погасить:  sudo containerlab destroy -t $TOPO"
