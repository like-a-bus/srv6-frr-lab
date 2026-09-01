# SRv6-лаба на FRR: IPv4 поверх SRv6

Готовая лаба на [Containerlab](https://containerlab.dev).

Собрана как дополнение к [srv6.md](https://srv6.md) — там отличная теория и разбор поведений, но конфигурацию под конкретную лабу приходится собирать самому по нескольким страницам. Здесь она уже собрана и проверена.

## Быстрый старт

```bash
git clone https://github.com/like-a-bus/srv6-frr-lab
cd srv6-frr-lab
bash setup.sh
```

Скрипт проверит окружение, скачает образ FRR, поднимет топологию и пропингует `10.0.3.1` с r1 через SRv6-туннель.

Погасить:

```bash
sudo containerlab destroy -t srv6-lab.clab.yml
```
## Что требуется

- Docker
- Containerlab — `bash -c "$(curl -sL https://get.containerlab.dev)"`
- Ядро с поддержкой SRv6 и VRF

На обычной Ubuntu, Debian или Fedora всё нужное уже есть. Проверить:

```bash
sudo ip -6 route add fc00:9::1/128 encap seg6local action End dev lo && echo OK
sudo ip -6 route del fc00:9::1/128
```

Если команда падает с `CONFIG_LWTUNNEL is not enabled in this kernel`, ядро не умеет SRv6. Это состояние по умолчанию в WSL2 — ядро от Microsoft собрано без `CONFIG_LWTUNNEL`. Варианты: запускать лабу в обычной VM либо пересобрать ядро WSL, см. [ниже](#wsl2).

## Топология

```
    fc00:12::/64        fc00:23::/64
r1 ---------------- r2 ---------------- r3
```

| Узел | Loopback | Локатор | End.DT4 SID | IPv4 в VRF |
| ---- | -------- | ------- | ----------- | ---------- |
| r1 | `fc00::1/128` | `fc00:1::/64` | `fc00:1:0:0:100::` | `10.0.1.1/32` |
| r2 | `fc00::2/128` | — | — | — |
| r3 | `fc00::3/128` | `fc00:3::/64` | `fc00:3:0:0:100::` | `10.0.3.1/32` |

r2 — обычный транзитный IPv6-роутер, никакой конфигурации SRv6 у него нет. Он просто доставляет пакет по внешнему заголовку.

## Как проверить, что трафик реально в туннеле

```bash
# SID в таблице ядра, proto 196 = поставлен FRR
sudo docker exec clab-srv6-lab-r3 ip -6 route show | grep seg6local

# политика инкапсуляции внутри VRF
sudo docker exec clab-srv6-lab-r1 ip route show vrf vrf1

# инкапсуляция на проводе
PID=$(sudo docker inspect -f '{{.State.Pid}}' clab-srv6-lab-r2)
sudo nsenter -t $PID -n tcpdump -ni eth1 -v 'ip6 proto 43'
```

В дампе видно внешний IPv6-заголовок с адресом SID, routing header типа 4 и исходный IPv4-пакет внутри:

```
IP6 fc00:12::1 > fc00:3:0:0:100:: RT6 (type=4, segleft=0, [0]fc00:3:0:0:100::)
  IP 10.0.1.1 > 10.0.3.1: ICMP echo request
```

Пинг обязательно запускать через `ip vrf exec vrf1` — источник должен быть внутри VRF, иначе лукап уйдёт в основную таблицу.

## Что здесь неочевидного


**Образ.** `frrouting/frr:latest` на Docker Hub не обновлялся с 2022 года, и в той версии `isisd` не понимает SRv6 — блок `segment-routing` отбрасывается при разборе конфига. Берём образ из Quay, здесь зафиксирован `10.7.1`.

**`net.vrf.strict_mode=1`.** Без него ядро отказывается создавать `End.DT4`: `Strict mode for VRF is disabled`.

**IPv4-адрес внутри VRF.** Нужен dummy-интерфейс, привязанный к `vrf1`, с адресом на нём. Если IPv4 в VRF нет, `zebra` зарегистрирует SID у себя, покажет его в `show segment-routing srv6 sid` — но в таблицу маршрутизации ядра не отдаст. Ошибки при этом нигде не будет.

**`vtysh -b` последней командой.** FRR читает `frr.conf` при старте контейнера, то есть до того, как Containerlab выполнит блок `exec` и создаст VRF. Повторное чтение конфига после подготовки окружения ставит всё на место.

**Переустановка SRv6-политик после сходимости.** По той же причине маршрут с `segments` применяется, когда IS-IS ещё не сошёлся и SID следующего узла не резолвится. FRR не отбрасывает такой маршрут, а тихо направляет его в менеджмент-интерфейс — выглядит как рабочая политика, но трафик уходит не туда. `setup.sh` дожидается появления локатора соседа и переустанавливает политики.

Плюс `seg6_enabled` нужно включать на конкретном интерфейсе: значения `all` и `default` не наследуются интерфейсами, которые Containerlab создаёт уже после старта контейнера.

## WSL2

Ядро WSL собрано без `CONFIG_LWTUNNEL`, поэтому SRv6 там не работает из коробки. Пересборка занимает 20–40 минут:

```bash
sudo apt update
sudo apt install -y build-essential flex bison libssl-dev libelf-dev \
                    bc dwarves python3 cpio pahole git

git clone --depth 1 -b linux-msft-wsl-6.6.y \
    https://github.com/microsoft/WSL2-Linux-Kernel.git
cd WSL2-Linux-Kernel
cp Microsoft/config-wsl .config

scripts/config --enable CONFIG_LWTUNNEL
scripts/config --enable CONFIG_IPV6_SEG6_LWTUNNEL
scripts/config --enable CONFIG_IPV6_SEG6_HMAC
scripts/config --enable CONFIG_NET_VRF
scripts/config --enable CONFIG_DUMMY
scripts/config --enable CONFIG_IKCONFIG
scripts/config --enable CONFIG_IKCONFIG_PROC
make olddefconfig

make -j$(nproc)
sudo make modules_install
sudo depmod -a
cp arch/x86/boot/bzImage /mnt/c/Users/ИМЯ/bzImage-srv6
```

Ветку выбирай по текущему `uname -r`. Затем в Windows создать `C:\Users\ИМЯ\.wslconfig`:

```ini
[wsl2]
kernel=C:\\Users\\ИМЯ\\bzImage-srv6
```

Двойные слеши обязательны. Дальше `wsl --shutdown` в PowerShell и заход заново.

Меняется только ядро — файловая система, пакеты и настройки остаются на месте. Откат: убрать строку `kernel=` и снова `wsl --shutdown`.

После смены ядра модули нужно подгружать явно, иначе docker не стартует:

```bash
printf 'bridge\nbr_netfilter\nxt_addrtype\nnft_compat\nvrf\ndummy\n' \
  | sudo tee /etc/modules-load.d/docker-srv6.conf
sudo modprobe bridge br_netfilter vrf dummy
```

Docker ставить из apt, не из snap: snap-версия работает в confinement и не видит bind-mount пути. Работать нужно в домашнем каталоге Linux, не в `/mnt/c/` — на drvfs нет нормальных прав, и FRR откажется читать конфиги.

На самосборном ядре часть netfilter-модулей для NAT не регистрируется, и docker падает при старте с `Extension MASQUERADE revision 0 not supported`. Лечится отключением iptables у демона:

```bash
echo '{"iptables": false, "ip6tables": false}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

Для лабы это безопасно: Containerlab соединяет узлы veth-парами напрямую, docker NAT в этом не участвует. Теряется только проброс портов наружу, который здесь не нужен.

## Ссылки

- [srv6.md](https://srv6.md) — теория SRv6, поведения, реализации
- [srv6.md: Linux Kernel](https://srv6.md/implementations/linux-kernel/) — синтаксис `iproute2` для seg6local
- [srv6.md: FRRouting](https://srv6.md/implementations/frrouting/) — статус поддержки SRv6 в FRR
- [Containerlab](https://containerlab.dev)
- [segmentrouting/srv6-labs](https://github.com/segmentrouting/srv6-labs) — более крупные сценарии, в основном под Cisco XRd

## Лицензия

MIT
