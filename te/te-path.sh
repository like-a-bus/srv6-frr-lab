#!/usr/bin/env bash
#
# Ставит на PE1 явный путь до 10.2.0.1/32 через заданные транзитные узлы.
#
# Номера функций End.X isisd выдаёт в порядке подъёма соседств, поэтому между
# передеплоями они меняются. Скрипт каждый раз собирает сеглист заново:
# для каждой пары "узел -> следующий узел" находит интерфейс из
# `show isis neighbor`, затем сид этого интерфейса из
# `show segment-routing srv6 sid`.
#
set -euo pipefail

LAB="${LAB:-clab-srv6-lab2}"
PATH_NODES="${PATH_NODES:-p1 p2 p4 p3}"   # транзит, по порядку
INGRESS="${INGRESS:-pe1}"                 # где ставим маршрут
EGRESS="${EGRESS:-pe3}"                   # чей DT4-сид последний
PREFIX="${PREFIX:-10.2.0.1/32}"
VRF="${VRF:-RED}"
OIF="${OIF:-eth3}"                        # интерфейс в той же VRF

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
inf() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ex()  { sudo docker exec "$LAB-$1" "${@:2}"; }

# сид End.X узла $1, смотрящий на соседа $2
adj_sid() {
  local node="$1" peer="$2" iface sid
  iface=$(ex "$node" vtysh -c "show isis neighbor" 2>/dev/null \
          | awk -v p="$peer" '$1 == p {print $2; exit}')
  [ -n "$iface" ] || { red "$node: соседство с $peer не найдено"; return 1; }
  sid=$(ex "$node" vtysh -c "show segment-routing srv6 sid" 2>/dev/null \
        | awk -v i="'$iface'" '$2 == "End.X" && $4 == i {print $1; exit}')
  [ -n "$sid" ] || { red "$node: нет End.X для $iface"; return 1; }
  echo "$sid"
  printf '    %-4s -> %-4s  %-8s %s\n' "$node" "$peer" "$iface" "$sid" >&2
}

inf "Собираю сеглист: $INGRESS -> $(echo $PATH_NODES | tr ' ' '-') -> $EGRESS"

read -r -a NODES <<< "$PATH_NODES"
SEGS=()

# для каждого транзитного узла, кроме последнего, берём сид на следующего
for ((i = 0; i < ${#NODES[@]} - 1; i++)); do
  SEGS+=("$(adj_sid "${NODES[i]}" "${NODES[i+1]}")")
done
# последний транзитный узел доносит пакет до DT4 обычной маршрутизацией,
# поэтому его adjacency-SID не нужен

inf "DT4-сид на $EGRESS"
DT4=$(ex "$EGRESS" vtysh -c "show segment-routing srv6 sid" 2>/dev/null \
      | awk -v v="'$VRF'" '$2 == "End.DT4" && $4 == v {print $1; exit}')
[ -n "$DT4" ] || { red "не нашёл End.DT4 для VRF $VRF на $EGRESS"; exit 1; }
printf '    %s\n' "$DT4"
SEGS+=("$DT4")

LIST=$(IFS=/; echo "${SEGS[*]}")
inf "Сеглист: $LIST"

inf "Ставлю маршрут на $INGRESS"
ex "$INGRESS" vtysh -c "conf t" \
   -c "ip route $PREFIX $OIF segments $LIST vrf $VRF"

inf "Проверяю"
ex "$INGRESS" ip route show vrf "$VRF" "${PREFIX%/*}" || true

echo
grn "Готово. Путь задан, но пинг сам по себе его не подтверждает —"
grn "проверять дампом: bash te/capture.sh"
