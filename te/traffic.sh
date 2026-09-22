#!/bin/bash
# Постоянный трафик h1 -> h2 через iperf3, для тестов сходимости.
#
#   bash te/traffic.sh start    запустить (по умолчанию UDP 100 Мбит/с, без ограничения по времени)
#   bash te/traffic.sh stop     остановить и показать итог сервера
#   bash te/traffic.sh status   идёт ли трафик, последние интервалы
#   bash te/traffic.sh log      интервалы сервера по 100 мс в реальном времени, выход Ctrl+C
#
# Параметры переменными: RATE=200M (суммарно), PROTO=tcp, STREAMS=1 (по умолчанию 4).
# Несколько потоков нужны, чтобы трафик разошёлся по ECMP: у них разные порты,
# а значит, разный хэш на CE и на PE и разный flow label во внешнем заголовке.
# Оба iperf3 работают внутри контейнеров (docker exec -d), терминал не занимают.
# Лог сервера — /tmp/iperf-server.log внутри h2, потери по интервалам смотреть там.

set -u

LAB="${LAB:-clab-srv6-lab2}"
RATE="${RATE:-100M}"
PROTO="${PROTO:-udp}"
STREAMS="${STREAMS:-4}"
DST=10.2.0.2
SLOG=/tmp/iperf-server.log
CLOG=/tmp/iperf-client.log

ex()      { sudo docker exec "$LAB-$1" "${@:2}"; }
running() { ex "$1" pgrep -x iperf3 >/dev/null 2>&1; }

stop_all() {
  # SIGINT, а не SIGTERM: так iperf3 успевает дописать итог
  ex h1 pkill -INT -x iperf3 >/dev/null 2>&1
  sleep 1
  ex h2 pkill -INT -x iperf3 >/dev/null 2>&1
  sleep 1
  ex h1 pkill -KILL -x iperf3 >/dev/null 2>&1
  ex h2 pkill -KILL -x iperf3 >/dev/null 2>&1
}

case "${1:-}" in
  start)
    if running h1; then
      echo "Трафик уже идёт. Сначала: bash te/traffic.sh stop"
      exit 1
    fi
    stop_all
    ex h2 rm -f "$SLOG"
    ex h1 rm -f "$CLOG"
    sudo docker exec -d "$LAB-h2" iperf3 -s -i 0.1 --forceflush --logfile "$SLOG"
    sleep 1
    if [ "$PROTO" = tcp ]; then
      args=(-P "$STREAMS")
      what="TCP, потоков: $STREAMS"
    else
      # -b в iperf3 задаётся на поток, а RATE у нас суммарный: делим
      num=${RATE%[KMGkmg]}
      suf=${RATE#$num}
      args=(-u -b "$(awk -v n="$num" -v s="$STREAMS" 'BEGIN{printf "%.4g", n/s}')$suf" -P "$STREAMS")
      what="UDP $RATE, потоков: $STREAMS"
    fi
    sudo docker exec -d "$LAB-h1" iperf3 -c "$DST" -t 0 -i 1 "${args[@]}" --forceflush --logfile "$CLOG"
    sleep 2
    if running h1 && running h2; then
      echo "Идёт: h1 -> h2 ($DST), $what, без ограничения по времени."
      echo "Остановить: bash te/traffic.sh stop"
    else
      echo "Не запустилось. Клиент:"
      ex h1 tail -5 "$CLOG"
      stop_all
      exit 1
    fi
    ;;
  stop)
    stop_all
    echo "Остановлено. Итог сервера:"
    ex h2 sh -c "grep -E 'receiver|Lost/Total' $SLOG | tail -3" 2>/dev/null || true
    ;;
  status)
    if running h1; then echo "Трафик идёт"; else echo "Трафика нет"; fi
    ex h2 tail -3 "$SLOG" 2>/dev/null || true
    ;;
  log)
    sudo docker exec -it "$LAB-h2" tail -f "$SLOG"
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
