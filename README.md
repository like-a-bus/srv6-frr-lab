# SRv6 на FRR

Две лабы на containerlab и FRR. Требуется ядро Linux 5.11 или новее
с CONFIG_LWTUNNEL и CONFIG_IPV6_SEG6_LWTUNNEL. Инструкция по пересборке
ядра под WSL2 — в [basic/README.md](basic/README.md).

## [basic](basic/) — три узла

PE1 — P — PE2. IS-IS в underlay, BGP L3VPN поверх, две VRF (RED и BLUE),
изолированные друг от друга. Минимальная схема, чтобы увидеть, как
IPv4-пакет едет внутри IPv6 и как SID распаковывается в нужную VRF.

## [te](te/) — десять узлов

2 CE, 4 PE, 4 P. CE подключены к двум PE каждый, active/active.
Два route reflector'а на P1 и P3, ядро без full-mesh. IS-IS с локаторами
на всех восьми узлах ядра — задел под управление трафиком через SRH.

Статических маршрутов нет ни в одной из лаб: underlay строит IS-IS,
маршруты VRF разносит BGP, SID выдаются автоматически.
