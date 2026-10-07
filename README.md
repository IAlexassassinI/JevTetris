# Jev грає в Тетріс

Агент, який у реальному часі грає в консольний Тетріс. Кожну дію обирає
System One модель **Open-Jev-2B**. Модель отримує поточний стан гри як текст і
вибирає одну з дозволених дій: рух фігури вліво/вправо, поворот, очікування або
кидок.

## Модель

Використана модель **Open-Jev-2B** ([ZefanCai/Open-Jev-2B](https://huggingface.co/ZefanCai/Open-Jev-2B)),
відкрита реалізація ідеї Jev від TypeSafe. Це LoRA-адаптер і окрема «decision
head» поверх Qwen3.5-2B. Модель не генерує текст: вона отримує стан, питання та
перелік варіантів і повертає ймовірності. Використовуються два типи питань:

- **choice**: розподіл ймовірностей між варіантами (тут це вибір дії);
- **noul**: ймовірність відповіді «так» на питання так/ні (тут «чи опускати фігуру зараз»).

Модель запускається локально (Docker) і приймає запити на
`http://127.0.0.1:8791/v1/systemone`. Кожен варіант модель оцінює як окрему
послідовність «стан + інструкція + варіант», ліміт — 4096 токенів на послідовність.

## Структура репозиторію

У репозиторії є **дві варіації промптів у двох теках**. Код гри, бота та
допоміжні скрипти в них однакові, відрізняються промпти й налаштування
(`tetris_prompt.py`, `config.json`):

| Тека | Варіант | Коротко |
|---|---|---|
| `prompt_full/` | повний | Довга інструкція з покроковою стратегією; питання `stop` містить повні правила і поле після кидка; серед дій є `soft_drop` |
| `prompt_short/` | скорочений (швидший) | Інструкція стиснута до ~500 символів; питання `stop` коротке, без повтору правил і поля; без `soft_drop`. Обсяг одного рішення ≈ 19 тис. символів замість ≈ 29 тис. |

Файли в кожній теці:

| Файл | Призначення |
|---|---|
| `tetris.py` | Гра: консольний інтерфейс + HTTP API (`/state`, `/move`, `/reset` …) |
| `tetris_jev_bot.py` | Агент: стан → запит до Jev → дія → новий стан … |
| `tetris_prompt.py` | Побудова промпту: текст стану, інструкції, варіанти дій, передбачення |
| `jev_client.py` | HTTP-клієнт до `/v1/systemone` |
| `config.json` | Налаштування агента |
| `mock_jev_server.py` | Фейковий сервер для перевірки без моделі (випадкові відповіді) |

## Запуск

Для запуску потрібен **запущений Open-Jev-2B**. Найпростіше підняти його з
репозиторію [Open-Jev](https://github.com/Zefan-Cai/Open-Jev) через Docker:

```
docker compose up -d --build                  # з NVIDIA GPU
docker compose up -d --build open-jev-cpu     # без GPU (значно повільніше)
docker compose ps                             # чекати статусу healthy
```

Далі з теки обраного варіанту (`prompt_full/` або `prompt_short/`), у двох терміналах:

```
python tetris.py --start-frames-per-row 60 --lock-delay 120
python tetris_jev_bot.py --config config.json
```

- `--start-frames-per-row 60`: на старті фігура опускається на рядок раз на
  секунду (60 кадрів при 60 FPS), далі гра поступово прискорюється з рівнем.
- `--lock-delay 120`: фігура, що лягла, фіксується через 2 с.

Вікно гри служить для перегляду; клавішами рухати фігуру не варто, щоб не
заважати агенту (`p` — пауза, `q` — вихід).

## Як грає агент

1. Бот читає стан гри (`GET /state`). Гра **не ставиться на паузу**: поки
   модель думає, фігура падає.
2. Будує запит з двома питаннями й надсилає його одним викликом:
   - `stop` (noul): чи опускати фігуру прямо зараз;
   - `action` (choice): яку дію зробити, якщо не опускати
     (`left`, `right`, `rotate_cw`, `rotate_ccw`, `wait`, у повному варіанті ще `soft_drop`).
3. Якщо p(«так») для `stop` ≥ 0.5, виконується `hard_drop`, інакше обрана дія.
4. Якщо фігура зафіксувалась раніше, ніж прийшла відповідь, дія відкидається
   (`stale`) і не застосовується до наступної фігури.

Усі рішення приймає тільки модель, евристик-замінників немає. Очки гра нараховує
тільки за очищені ряди: 1/2/3/4 ряди = 40/100/300/1200 × (рівень + 1).

## Промпти

### Стан (однаковий в обох варіантах)

Поле подається як є, без підрахунку дірок і висот, але з передбаченнями, де
фігура опиниться. Приклад (фігура T щойно з'явилась над двома майже повними рядами):

```
Tetris board, 10 columns (0-9) x 20 rows (row 0 is the top, row 19 is the bottom).
'#' = locked block, '@' = falling piece, '.' = empty, 'o' = where the piece would land after hard_drop.
    0 1 2 3 4 5 6 7 8 9
 0  . . . . @ . . . . .
 1  . . . @ @ @ . . . .
 ...
16  . . . . o . . . . .
17  # # . o o o . . . .
18  # # # # . . # # # #
19  # # # # . . # # # #
Falling piece: T, rotation 0, at rows 0-1, columns 3-5; it is 16 row(s) above where it would land
(still high: there is time to search for a better place).
Drop predictions:
- one row lower (next fall step): rows 1-2, columns 3-5.
- after hard_drop (to the bottom): lands at rows 16-17, columns 3-5, would complete no rows.
Possible orientations of this T piece (rotate_cw: +1, rotate_ccw: -1; four rotations bring it back to the start):
- rotation 1 (reached with 1x rotate_cw): if rotated into it now and dropped: lands at rows 17-19,
  columns 4-5, would complete 1 row(s) = 40 points
    @ .
    @ @
    @ .
  ...
Previous step: none, this piece has just appeared.
Next pieces: Z, J, L.
Score 0, level 0, rows cleared 0.
```

Складові стану:

- поле з номерами рядів і колонок; клітинки розділені пробілами, щоб кожна була
  окремим токеном; `o` позначає, де фігура ляже при кидку;
- позиція фігури та на скільки рядів вона вища за місце приземлення
  (з 6+ рядів — підказка, що є час шукати місце);
- передбачення: де фігура буде на рядок нижче і де ляже при кидку;
- усі орієнтації фігури: малюнок, скільки поворотів потрібно, де ляже після
  повороту й кидка (або що поворот заблокований);
- попередній крок цієї фігури: де вона була, що з нею зробили і чи змінилось
  від цього місце приземлення;
- наступні фігури, рахунок, рівень.

### Варіанти дій (однакові в обох варіантах)

Кожен варіант містить опис дії та передбачення: де фігура буде після дії і де
ляже, якщо одразу після цього її опустити. Приклад:

```
rotate_cw: Search/steer: rotate the falling piece 90 degrees clockwise, to try this orientation
or because its current orientation does not fit the place where it should land. Result: after
this action the piece will be at rows 0-2, columns 4-5. If it is dropped straight to the bottom
right after that, it lands at rows 17-19, columns 4-5, would complete 1 row(s) = 40 points.
```

`left`/`right` описані як пошук або підведення до обраного місця, `wait` — як
доречний лише тоді, коли фігура вже над своїм місцем. До кожного варіанту також
додано повне поле наступного стану.

### Загальна інструкція (питання `action`)

**Повний варіант** (~1300 символів):

> You are playing Tetris. GOAL: score as many points as possible. Points come from completing
> full horizontal rows of blocks, which then disappear: 1 row = 40, 2 rows at once = 100,
> 3 rows = 300, 4 rows = 1200 points, multiplied by (level + 1). LOSING: new pieces appear in
> rows 0-1, columns 3-6. If locked blocks reach row 1 there (a stack 19 rows high), the next
> piece cannot appear and the game is over, so keep the stack at most 18 rows high and as low
> as possible. Do not leave empty gaps under pieces: a row with a gap cannot be completed.
> Handle every falling piece '@' like this: 1) SEARCH EARLY: as soon as the piece appears,
> while it is still high and there is time, move it left and right (and try its rotations) and
> compare the predicted landings of the options to find the best place for it, before
> committing; 2) STEER: rotate it into the orientation that fits that place and move it
> directly above it; 3) only then DROP it. Do not drop a piece that has just appeared without
> having looked at other columns first. Rotating more than needed wastes time: after four
> rotations the piece is back where it started. Use the drop predictions to see where the
> piece would land. The piece is NOT in its final position yet: choose the move that searches
> for or steers toward a better place.

**Скорочений варіант** (~500 символів):

> Tetris. Goal: maximum score. Points come only from completed full rows: 1/2/3/4 rows at
> once = 40/100/300/1200 x (level+1). You lose if blocks reach row 1 in columns 3-6, so keep
> the stack low (at most 18 rows). Avoid empty gaps under pieces. For each piece '@': first,
> while it is high, move and rotate it to compare predicted landings; then steer it above the
> best place; then drop it. Four rotations = back to the start. Not dropping yet: pick the move
> that searches or steers toward a better place.

### Питання `stop` (noul)

**Повний варіант**: повна інструкція вище, а після неї:

> QUESTION: Is the falling piece already in a good final position, so it should be dropped
> right now? Look at where it would land (the hard_drop prediction and the 'o' marks) and at
> the board after the drop shown below. Answer yes if that landing completes rows, or lies flat
> on the surface without leaving empty gaps under it and without making the stack needlessly
> high. Answer no if rotating or moving the piece would clearly give a better landing, and also
> answer no if the piece has just appeared and is still high while other columns have not been
> tried yet: there is time to search first.
> If dropped now: lands at rows 16-17, columns 3-5, would complete no rows.
> *(далі повне поле після кидка: з очищеними рядами й новою фігурою)*

**Скорочений варіант**:

> Tetris: points only for full rows; avoid gaps; keep the stack low. QUESTION: Should the
> falling piece be dropped right now? Yes if its landing ('o') completes rows, or sits flat with
> no gaps under it and keeps the stack low. No if moving or rotating would clearly give a better
> landing, or if the piece has just appeared and other columns have not been tried yet.
> If dropped now: lands at rows 16-17, columns 3-5, would complete no rows.

Усі передбачення рахуються тими самими правилами (повороти, зсуви від стін,
очищення рядів), що й у грі, і звірені з реальною грою на тисячах ходів.

## Проблеми та спостереження

**Для коректної гри агенту фактично потрібно передбачити надто багато.**
Щоб добре поставити фігуру, треба порівняти всі її орієнтації в усіх
колонках і оцінити, яка форма поверхні вийде після кожного варіанту. Модель
бачить лише варіанти поточного кроку (один рух або один поворот) і оцінює кожен
окремо, без плану на кілька кроків уперед. Тому навіть з передбаченнями в
промпті їй доводиться за кілька послідовних рішень «зібрати» план, якого вона
явно не має.

**Агент сам по собі не встигає все просканувати й зробити дію.** На одну
фігуру потрібно 5–15 рішень, а кожне — окремий запит, у якому модель оцінює
6–7 довгих послідовностей. Гра в цей час не стоїть: поки агент обирає між
рухами, фігура падає.

**Агент надто вагається з діями.** Ймовірності рухів часто близькі між собою,
тож модель смикає фігуру вліво-вправо або крутить її, не доводячи до рішення.
Окреме питання `stop` частково це виправляє, але й воно часто відповідає
непевно.

**Модель упевнена лише в діях, що одразу ведуть до заповнення ряду.** Коли
передбачення прямо каже «would complete 1 row(s) = 40 points», модель обирає
цей варіант з високою впевненістю. Усе інше — рівна поверхня, відсутність дірок,
підготовка місця для наступних фігур — їй оцінити важко, хоча саме це становить
більшість ходів у Тетрісі. Через це гра йде добре лише тоді, коли ряд можна
закрити одним ходом.

Імовірні шляхи покращення: більша модель (Open-Jev-9B), вищий рівень рішень
(модель обирає ціль — колонку й поворот, а бот доводить фігуру), або поєднання
з LLM як System II для оцінки позицій, які System One оцінити не може.


Корисні налаштування (у `config.json` або прапорцями):
`--stop-threshold 0.5`, `--include-rotations false`, `--include-previous false`,
`--next-state-boards false`, `--show-ghost false`, `--games 5`, `--trace runs/name.jsonl`.

Перевірка без моделі: `python mock_jev_server.py --mode random --delay 0.4`
замість Open-Jev (відповіді випадкові, лише для перевірки зв'язки).
