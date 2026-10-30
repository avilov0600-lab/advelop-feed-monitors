# MASTERS feed monitor

Контроль материнского XML Capital Group, официального каталога квартир
МАСТЕРС и выгрузок для Циан, Яндекс Недвижимости, Авито, Домклик и М2.

Монитор:

- использует клиентский XML Capital Group как материнский источник;
- выделяет из него квартиры со статусом, предназначенным для публикации;
- сопоставляет пять площадочных XML с материнским источником по UUID лота;
- показывает официальный JSON-каталог сайта только справочно; данные сайта не
  влияют на статус товарных фидов и не используются для проверки этажности;
- нормализует базовую и скидочную цену Яндекса;
- проверяет количество, дубли, площадь, комнатность, этаж и сопоставимую
  этажность, не смешивая секционные и общедомовые поля разных схем;
- показывает различия состава материнского XML и сайта отдельно от ошибок
  выгрузки;
- проверяет устаревшие рекламные формулировки;
- проверяет объявления через API Циана и Авито и публичные карточки ЖК на
  Яндекс Недвижимости и М2;
- считает Домклик синхронизированным с площадкой, если его XML содержит столько
  же лотов, сколько остальные товарные фиды; при расхождении требует ручной
  проверки площадки;
- там, где площадка публикует доступное для автоматического чтения количество
  предложений, сопоставляет его с соответствующим XML-фидом;
- формирует короткое сообщение для Telegram.

## Локальный запуск

```bash
python3 monitor.py --cache-dir .cache --output reports/latest.json
```

Для анализа уже скачанных файлов:

```bash
python3 monitor.py --offline --cache-dir /private/tmp --output reports/latest.json
```

В каталоге должны лежать `masters-master.xml`, `masters-site.json` и файлы
`masters-cian.xml`, `masters-yandex.xml`, `masters-avito.xml`,
`masters-domclick.xml`, `masters-m2.xml`.

## Telegram

Создайте бота через BotFather, добавьте его в нужный чат и передайте секреты
через переменные окружения:

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='...'
python3 monitor.py --cache-dir .cache --send-telegram
```

Токен не следует добавлять в репозиторий или конфигурационные файлы. По
умолчанию проблема должна повториться два запуска подряд, после чего бот
отправит один алерт. Повторное сообщение придёт только при изменении набора
ошибок или после их устранения. Состояние хранится в `.state.json`.

Последний дайджест и список подписчиков сохраняются в закрытом Object Storage,
без имён и других данных профиля. Когда новый пользователь открывает бота в
личном чате и нажимает `/start`, Telegram webhook сразу отдаёт ему последний
текст без повторной проверки и подключает дальнейшие автоматические обновления.
Команда `/stop` отключает их. Пользователи, которые нажимали `/start` до запуска
этой функции, должны нажать команду ещё раз: прежняя версия не сохраняла список.

## Расписание

Рабочее расписание задано в `.github/workflows/monitor.yml`:

- сравнение запускается четыре раза в сутки: **09:00, 13:00, 15:30 и 18:30 МСК**;
- после каждого планового запуска отправляется текущая сводка в Telegram,
  даже если набор ошибок не изменился; её получают все активные подписчики;
- публичный Cloudflare relay принимает Telegram webhook и передаёт его в
  Yandex Cloud, где новый пользователь сразу получает последний сохранённый
  дайджест;
- защита из двух последовательных запусков и подавление повторов остаются
  только для ручного режима `monitor`.

В GitHub Actions используются UTC-расписания `0 6,10 * * *` и `30 12,15 * * *`. Это время
планового старта, а не гарантия доставки в ту же минуту: возможны задержки
очереди GitHub и время на проверку источников.

В приватном репозитории должны быть заданы пять GitHub Actions secrets:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CIAN_API_KEY`, `AVITO_CLIENT_ID`
и `AVITO_CLIENT_SECRET`. Полный JSON-отчёт сохраняется в
Artifacts конкретного запуска на 1 день, а короткая сводка отправляется в
Telegram.

Каждый основной XML загружается максимум три раза с паузой 2 секунды. Если
один из пяти товарных XML остаётся недоступен, отчёт получает статус
`SOURCE_UNAVAILABLE`, продолжает проверку остальных источников и не подменяет
недоступный источник ложным значением «0 лотов».

Ручной запуск доступен на странице Actions. Режим `digest` всегда отправляет
сообщение, режим `monitor` использует обычную защиту от повторов, режим `sync`
только обновляет кэш для `/start` из последнего сохранённого JSON без рассылки.

## Yandex Cloud

Готовая схема развёртывания находится в `yandex_cloud/`:

- Cloud Function на Python 3.12 запускает тот же монитор;
- один триггер отправляет сводку в 09:00 и 13:00 МСК;
- второй триггер отправляет сводку в 15:30 и 18:30 МСК;
- закрытый Object Storage-бакет смонтирован в функцию и хранит только
  `alert-state.json`, `latest-report.json` и последний текст Telegram;
- функция принимает только подписанные Telegram webhook события и
  авторизованную синхронизацию последнего сообщения;
- публичный relay не хранит токен и только передаёт Telegram-подпись и тело
  запроса в функцию;
- токен Telegram передаётся из Lockbox и не попадает в код или ZIP-архив.

Перед развёртыванием создайте Telegram-бота и секрет Lockbox с ключом
`telegram_bot_token`. Затем установите и инициализируйте `yc`, задайте
переменные и выполните скрипт:

```bash
export YC_FOLDER_ID='...'
export YC_STATE_BUCKET='masters-feed-monitor-unique-name'
export YC_TELEGRAM_SECRET_ID='...'
export YC_TELEGRAM_SECRET_VERSION_ID='...'
export TELEGRAM_CHAT_ID='...'
export TELEGRAM_WEBHOOK_URL='https://your-relay.example/telegram'
bash yandex_cloud/deploy.sh
```

Скрипт создаёт или обновляет сервисный аккаунт, закрытый бакет, функцию и два
таймера. Расписания Yandex Cloud задаются в UTC: `0 6,10 ? * * *` для 09:00/13:00 и
`30 12,15 ? * * *` для 15:30/18:30 МСК. Оба таймера используют режим
`digest`, чтобы каждый плановый запуск отправлял сводку. Старые имена
триггеров сохранены для обновления существующих ресурсов без создания дублей.

Таймеры Yandex Cloud — альтернативный планировщик. Не включайте их одновременно
с расписанием GitHub Actions, иначе рассылки будут дублироваться. Обновление
скрипта само по себе не меняет уже развёрнутые таймеры в облаке.

## Четыре уровня контроля

Сообщение Telegram разделяет источники и больше не называет XML-фид данными
площадки:

1. `Материнский XML Capital Group ↔ наши XML` — основной контроль состава и
   проверяемых атрибутов по UUID. Сырые значения этажности разных площадочных
   схем не сравниваются между собой.
2. `Сайт Capital Group ↔ материнский XML` — вторичный контроль по базовой цене,
   площади, комнатности и этажу, поскольку сайт не раскрывает UUID.
3. `XML-фид ↔ площадка` — Циан проверяется через API по числу опубликованных
   объявлений и статусам импорта, Авито — по числу активных объявлений проекта
   с адресом Викторенко, Домклик — по равенству его XML остальным фидам.
4. Публичные карточки ЖК — наличие и количество предложений у остальных
   площадок, если страница раскрывает их в доступной форме.

Цифровой `externalId` API Циана не совпадает с UUID исходного XML, а API списка
Авито также не возвращает внешний ID из XML. Поэтому для обеих площадок бот
честно показывает агрегатную проверку количества; для Циана дополнительно
считываются статусы последнего импорта. Для Домклик действует согласованное
правило: равное количество во всех товарных XML означает `площадка ОК`, любое
расхождение даёт красный статус `площадку требуется проверить`.

## Website availability in reports

Each regular report now includes the public Capital Group catalogue HTTP status
next to the website lot count. The check performs GET requests without reading
the response body, follows redirects and verifies TLS. Reports use up to three
attempts, an 8-second request timeout and 2-second retry pauses. No separate
Telegram outage messages, status database or additional schedule is created.
A successful HTTP response does not confirm that the lot catalogue was parsed;
missing inventory remains unknown rather than zero. The maternal XML remains
authoritative and availability does not alter feed comparison results.

For operational use, `--refresh-site-availability --output reports/latest.json`
refreshes only this status in an existing saved report, retaining the original
feed check time and counts. It does not request XML feeds or marketplace APIs.
The website row has its own observation time. `/start` still returns saved text.

## Concise report format

Public website reachability appears first: ✅ Сайт доступен or ❌ Сайт недоступен
with a brief reason. Unknown checks stay explicitly unverified. Counts from the
client source, website and outbound feeds are grouped; differences in IDs, fields,
promotional copy and publication remain visible in plain language. Cian and Avito
publication totals remain scoped to their actual API accounts.

Cian yesterday statistics are displayed only in the morning report (09:00–11:59
Europe/Moscow, allowing normal execution delay). Other reports retain the saved
statistics data but omit that presentation block. Parsers, API calls, daily cache,
subscriptions and the existing schedule are unchanged.
