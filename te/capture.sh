#!/bin/bash
# Захват SRv6-трафика по всем узлам лабы, по одному pcap на интерфейс.
# Пишет Ethernet link-type, поэтому открывается любым Wireshark.

set -u

LAB="${LAB:-clab-srv6-lab2}"
NODES=${NODES:-"ce1 pe1 pe2 p1 p2 p3 p4 pe3 pe4 ce2"}
DIR="${DIR:-$(cd "$(dirname "$0")" && pwd)/pcap}"
WIN=${WIN:-/mnt/c/Users/Admin/pcap}
COUNT=${COUNT:-5}

echo "==> Каталог: $DIR"
mkdir -p "$DIR"
rm -f "$DIR"/*.pcap

echo "==> Запускаю захват"
STARTED=0
for n in $NODES; do
  PID=$(sudo docker inspect -f '{{.State.Pid}}' "$LAB-$n" 2>/dev/null)
  if [ -z "$PID" ]; then
    echo "    $n — контейнер не найден, пропускаю"
    continue
  fi
  for i in $(sudo nsenter -t "$PID" -n ip -o link show | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -v '^lo$'); do
    sudo nsenter -t "$PID" -n tcpdump -ni "$i" -U -w "$DIR/$n-$i.pcap" 'ip6 or icmp' >/dev/null 2>&1 &
    STARTED=$((STARTED + 1))
  done
done
echo "    интерфейсов под захватом: $STARTED"

sleep 3

echo "==> Пинг CE1 -> CE2"
sudo docker exec "$LAB-ce1" ping -c "$COUNT" -i 0.5 -I 10.1.0.1 10.2.0.1

sleep 2
sudo pkill -f "tcpdump -ni" >/dev/null 2>&1
sleep 1

echo
echo "==> Файлы с трафиком (пустые удалены)"
find "$DIR" -name '*.pcap' -size -30c -delete
ls -lh "$DIR" | tail -n +2

echo
echo "==> Кто что видел"
for f in "$DIR"/*.pcap; do
  [ -e "$f" ] || continue
  echo "----- $(basename "$f" .pcap) -----"
  sudo tcpdump -nr "$f" 2>/dev/null | head -4
done

if [ -d "$(dirname "$WIN")" ]; then
  mkdir -p "$WIN"
  rm -f "$WIN"/*.pcap
  cp "$DIR"/*.pcap "$WIN"/ 2>/dev/null
  echo
  echo "==> Скопировано в $WIN"
fi
