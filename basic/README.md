# SRv6 на FRR

Дополнение к [srv6.md](https://srv6.md).

## Лаба

```
    fc00:12::/64        fc00:23::/64
r1 ---------------- r2 ---------------- r3
```

Underlay — IS-IS: он разносит loopback'и и локаторы. Overlay — BGP L3VPN (VPNv4)
поверх iBGP-сессии r1—r3, поднятой по loopback'ам. Статических маршрутов и
статических SID в конфигурации нет: SID выдаёт BGP, префиксы разносит BGP.

r2 — чистый транзит: ни BGP, ни SRv6 он не знает, он доставляет пакет по
внешнему IPv6-заголовку.

| Узел | Loopback | Локатор | BGP |
| ---- | -------- | ------- | --- |
| r1 | `fc00::1/128` | `MAIN` = `fc00:1::/64` | AS 65000, сосед `fc00::3` |
| r2 | `fc00::2/128` | — | — |
| r3 | `fc00::3/128` | `MAIN` = `fc00:3::/64` | AS 65000, сосед `fc00::1` |

Две независимые VRF. Трафик между ними не смешивается: разные таблицы ядра,
разные RT, разные uDT4 SID.

| VRF | Таблица | RD на r1 / r3 | RT | IPv4 на r1 | IPv4 на r3 |
| --- | ------- | ------------- | -- | ---------- | ---------- |
| RED | 100 | `1:100` / `3:100` | `65000:100` | `10.0.1.1/32` | `10.0.3.1/32` |
| BLUE | 200 | `1:200` / `3:200` | `65000:200` | `10.1.1.1/32` | `10.1.3.1/32` |

`sid vpn export auto` выдаёт каждой VRF свой uDT4 SID из локатора `MAIN`.
IPv4-пакет из VRF инкапсулируется на r1 во внешний IPv6 с адресом назначения
= SID нужной VRF на r3, проходит r2 как обычный IPv6 и на r3 распаковывается
в ту VRF, которой принадлежит SID.

## 1. Containerlab

```bash
bash -c "$(curl -sL https://get.containerlab.dev)"
```

## 2. Docker

```bash
sudo apt install -y docker.io
sudo systemctl start docker
```

## 3. Ядро

Нужны `CONFIG_LWTUNNEL`, `CONFIG_IPV6_SEG6_LWTUNNEL`, `CONFIG_NET_VRF`, `CONFIG_DUMMY` и ядро 5.11+.

```bash
sudo ip -6 route add fc00:9::1/128 encap seg6local action End dev lo && echo OK
sudo ip -6 route del fc00:9::1/128
```

В Ubuntu и Debian всё уже включено. В WSL2 — нет, ядро придётся пересобрать, см. [WSL2](#wsl2).

## 4. Запуск

```bash
git clone https://github.com/like-a-bus/srv6-frr-lab
cd srv6-frr-lab
bash setup.sh
```

Погасить:

```bash
sudo containerlab destroy -t srv6-lab.clab.yml
```

## Проверка

```bash
# SID, выданные BGP: по одному uDT4 на VRF
sudo docker exec clab-srv6-lab-r1 vtysh -c "show segment-routing srv6 sid"

# сессия VPNv4
sudo docker exec clab-srv6-lab-r1 vtysh -c "show bgp ipv4 vpn summary"

# что приехало по VPNv4
sudo docker exec clab-srv6-lab-r1 vtysh -c "show bgp ipv4 vpn"

# SID в таблице ядра, proto 196 — поставлен FRR
sudo docker exec clab-srv6-lab-r3 ip -6 route show | grep seg6local

# маршруты и инкапсуляция в каждой VRF
sudo docker exec clab-srv6-lab-r1 ip route show vrf RED
sudo docker exec clab-srv6-lab-r1 ip route show vrf BLUE

# трафик
sudo docker exec clab-srv6-lab-r1 ip vrf exec RED  ping -c4 -I 10.0.1.1 10.0.3.1
sudo docker exec clab-srv6-lab-r1 ip vrf exec BLUE ping -c4 -I 10.1.1.1 10.1.3.1

# изоляция: из RED в BLUE ходить не должно
sudo docker exec clab-srv6-lab-r1 ip vrf exec RED ping -c1 -W2 -I 10.0.1.1 10.1.3.1

# инкапсуляция на проводе
PID=$(sudo docker inspect -f '{{.State.Pid}}' clab-srv6-lab-r2)
sudo nsenter -t $PID -n tcpdump -ni eth1 -v 'ip6 proto 43 or ip6 proto 4'
```

В дампе видно два разных адреса назначения — по одному на VRF:

```
IP6 fc00::1 > fc00:3:0:0:1::   IP 10.0.1.1 > 10.0.3.1: ICMP echo request
IP6 fc00::1 > fc00:3:0:0:2::   IP 10.1.1.1 > 10.1.3.1: ICMP echo request
```

## Что неочевидно

**Образ.** `frrouting/frr:latest` на Docker Hub — это FRR 8.4, там `isisd` не понимает SRv6 и отбрасывает конфиг при разборе. Образы из Quay, зафиксирован `10.7.1`.

**`net.vrf.strict_mode=1`.** Без него ядро не создаёт `End.DT4`: `Strict mode for VRF is disabled`.

**IPv4-адрес внутри VRF.** Без него `zebra` покажет SID в `show segment-routing srv6 sid`, но в таблицу ядра не отдаст. Без ошибок.

**`vtysh -b`.** FRR читает конфиг при старте контейнера, до того как Containerlab создаст VRF. Нужно перечитать после.

**Имя VRF-интерфейса.** `ip link add RED type vrf` и `vrf RED` в конфиге FRR — одно и то же имя, иначе FRR не свяжет их и SID не установится.

**`seg6_enabled`.** Включается на конкретном интерфейсе, значения `all` и `default` не наследуются интерфейсами, созданными после старта контейнера.

**Переустановка политик больше не нужна.** Со статикой маршрут применялся до сходимости IS-IS, SID не резолвился, и трафик уходил в менеджмент-интерфейс. BGP сам переоценивает next-hop, когда локатор соседа появляется в таблице, — `setup.sh` просто ждёт маршруты в VRF.

**Один SID на VRF, а не на пару.** `sid vpn export auto` под `address-family ipv4 unicast` даёт uDT4 на VRF. Если нужен один SID на IPv4 и IPv6 сразу — это `sid vpn per-vrf export auto` и поведение uDT46.

**RD и RT — разные вещи.** RD у каждой пары «роутер + VRF» свой (`1:100`, `3:100`), он только различает префиксы в общей VPNv4-таблице. Кто с кем обменивается маршрутами, решает RT: RED видит только `65000:100`, BLUE — только `65000:200`. Это и есть изоляция на уровне control plane.

## WSL2

Ядро от Microsoft собрано без `CONFIG_LWTUNNEL`.

Ветка исходников выбирается по текущему `uname -r`: для `5.15.x` — `linux-msft-wsl-5.15.y`, для `6.6.x` — `linux-msft-wsl-6.6.y`.

```bash
sudo apt install -y build-essential flex bison libssl-dev libelf-dev \
                    bc dwarves cpio pahole git

git clone --depth 1 -b linux-msft-wsl-6.6.y \
    https://github.com/microsoft/WSL2-Linux-Kernel.git
cd WSL2-Linux-Kernel
cp Microsoft/config-wsl .config

scripts/config --enable CONFIG_LWTUNNEL
scripts/config --enable CONFIG_IPV6_SEG6_LWTUNNEL
scripts/config --enable CONFIG_IPV6_SEG6_HMAC
scripts/config --enable CONFIG_NET_VRF
scripts/config --enable CONFIG_DUMMY
make olddefconfig

make -j$(nproc)
sudo make modules_install
sudo depmod -a
cp arch/x86/boot/bzImage /mnt/c/Users/ИМЯ/bzImage-srv6
```

`C:\Users\ИМЯ\.wslconfig`:

```ini
[wsl2]
kernel=C:\\Users\\ИМЯ\\bzImage-srv6
```

Двойные слеши обязательны. Дальше `wsl --shutdown` в PowerShell и заход заново, в `uname -r` появится `+`.

Меняется только ядро, файловая система и пакеты остаются на месте. Откат — убрать строку `kernel=` и снова `wsl --shutdown`.

### Возможные проблемы

`systemctl` не отвечает — в дистрибутиве выключен systemd:

```bash
printf '[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf
```

Затем `wsl --shutdown` и заход заново.

`apt update` виснет на `Ign:`, при этом `ping 8.8.8.8` работает — не резолвится DNS:

```bash
printf '[network]\ngenerateResolvConf = false\n' | sudo tee -a /etc/wsl.conf
sudo rm -f /etc/resolv.conf
printf 'nameserver 8.8.8.8\n' | sudo tee /etc/resolv.conf
```

Затем `wsl --shutdown` и заход заново.

Docker падает с `Extension MASQUERADE revision 0 not supported` — не хватает netfilter-модулей для NAT:

```bash
echo '{"iptables": false, "ip6tables": false}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

Если `CONFIG_NET_VRF` и `CONFIG_DUMMY` собрались модулями, а не вкомпилированы:

```bash
printf 'vrf\ndummy\n' | sudo tee /etc/modules-load.d/srv6.conf
sudo modprobe vrf dummy
```

Docker ставить из apt, не из snap — snap-версия не видит bind-mount пути. Работать в домашнем каталоге Linux, не в `/mnt/c/`.

## Ссылки

- [srv6.md](https://srv6.md)
- [srv6.md: Linux Kernel](https://srv6.md/implementations/linux-kernel/)
- [srv6.md: FRRouting](https://srv6.md/implementations/frrouting/)
- [Containerlab](https://containerlab.dev)

## Лицензия

MIT
