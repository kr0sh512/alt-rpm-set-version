## original set.c w/out optimizations

функция `decode_set` идёт по пути функций:

1. `decode_base62`
2. `decode_golomb`
3. `decode_delta`

| command                     | average_seconds | run1_seconds | run2_seconds | run3_seconds | exit_status |
| :-------------------------- | :-------------: | :----------: | :----------: | :----------: | ----------: |
| -s check                    |    4.217343     |   4.310882   |   4.179343   |   4.161804   |           0 |
| -s autoremove               |    4.884413     |   4.876135   |   4.882347   |   4.894757   |           0 |
| -s install rpm-build        |    4.634789     |   4.652499   |   4.623077   |   4.628792   |           0 |
| -s install openuds-server   |    12.704538    |  12.640266   |  12.975482   |  12.497867   |           0 |
| -s install password-store   |    8.560463     |   8.561515   |   8.557596   |   8.562277   |           0 |
| --enable-upgrade -s upgrade |    36.271838    |  35.511443   |  35.908641   |  37.395430   |         100 |
| -s dist-upgrade             |    90.550086    |  94.702198   |  92.174160   |  84.773901   |         100 |

## original set.c

функция `decode_set` идёт по пути функций:

1. `decode_base62_golomb`
2. `decode_delta`

| command                     | average_seconds | run1_seconds | run2_seconds | run3_seconds | exit_status |
| :-------------------------- | :-------------: | :----------: | :----------: | :----------: | ----------: |
| -s check                    |    1.570747     |   1.621496   |   1.545001   |   1.545744   |           0 |
| -s autoremove               |    2.294502     |   2.312574   |   2.287133   |   2.283798   |           0 |
| -s install rpm-build        |    2.108121     |   2.142784   |   2.093474   |   2.088105   |           0 |
| -s install openuds-server   |    8.028098     |   8.147214   |   8.001140   |   7.935940   |           0 |
| -s install password-store   |    3.373377     |   3.396943   |   3.361290   |   3.361899   |           0 |
| --enable-upgrade -s upgrade |    33.915979    |  33.895262   |  33.770192   |  34.082484   |         100 |
| -s dist-upgrade             |    88.722136    |  95.484250   |  85.808085   |  84.874073   |         100 |

## set9.c

Переписанная реализация `set.c`

| command                     | average_seconds | run1_seconds | run2_seconds | run3_seconds | exit_status |
| :-------------------------- | :-------------: | :----------: | :----------: | :----------: | ----------: |
| -s check                    |    1.550342     |   1.596307   |   1.532768   |   1.521950   |           0 |
| -s autoremove               |    2.200188     |   2.237645   |   2.185378   |   2.177540   |           0 |
| -s install rpm-build        |    2.031055     |   2.045757   |   2.023173   |   2.024234   |           0 |
| -s install openuds-server   |    7.580058     |   7.572994   |   7.582557   |   7.584622   |           0 |
| -s install password-store   |    3.337012     |   3.367696   |   3.309826   |   3.333514   |           0 |
| --enable-upgrade -s upgrade |    31.494775    |  31.535714   |  31.585522   |  31.363090   |         100 |
| -s dist-upgrade             |    85.326580    |  85.389775   |  85.604566   |  84.985398   |         100 |
