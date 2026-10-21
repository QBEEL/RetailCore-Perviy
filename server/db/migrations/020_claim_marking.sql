-- Торг-2: код маркировки и излишек.
--
-- Код маркировки (DataMatrix с упаковки) записывается в заявку, когда товар
-- маркируемый: по нему видно, какой именно экземпляр бракованный или лишний, и
-- он попадает на четвёртую страницу акта ТОРГ-2. Хранится строкой как её
-- отсканировали; пусто — товар не маркируемый или код не нужен (у недопоставки
-- товара на складе нет, и сканировать нечего).
--
-- Третий вид заявки — излишек: привезли больше, чем в документах.

ALTER TABLE claim
    ADD COLUMN IF NOT EXISTS mark_code TEXT NOT NULL DEFAULT '';

ALTER TABLE claim DROP CONSTRAINT IF EXISTS claim_kind_check;
ALTER TABLE claim
    ADD CONSTRAINT claim_kind_check
    CHECK (kind IN ('shortage', 'defect', 'surplus'));

-- Подсказка «этот товар маркируемый»: по штрих-коду ищется заявка с кодом.
CREATE INDEX IF NOT EXISTS claim_marked ON claim(barcode) WHERE mark_code <> '';
