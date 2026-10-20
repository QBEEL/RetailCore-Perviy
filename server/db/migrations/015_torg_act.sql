-- Акты ТОРГ-2, составленные из заявок Торг-2, и реквизиты для них.
--
-- Раньше акт склад набирал руками в Excel и прикладывал к строке таблицы.
-- Теперь акт собирается в приложении из выбранных заявок, а здесь хранится то,
-- из чего он собран: по нему акт можно открыть и поправить, а номер и поставщик
-- не набираются заново.
--
-- Всё, кроме номера, даты и поставщика, лежит в `payload` нетипизированным
-- jsonb: состав полей бланка меняется чаще, чем выкатывается сервер (так же
-- устроены профили отчётов, см. 003_reports.sql). Сам бланк собирает
-- приложение; сервер акт не рисует.

CREATE TABLE IF NOT EXISTS torg_act (
    id         BIGSERIAL PRIMARY KEY,
    -- Номер сквозной в пределах года: у каждого года счёт начинается заново.
    number     INTEGER     NOT NULL CHECK (number > 0),
    -- Год хранится отдельно от даты, чтобы уникальность была обычным
    -- ограничением, а не индексом по выражению.
    act_year   INTEGER     NOT NULL,
    act_date   DATE        NOT NULL,
    supplier   TEXT        NOT NULL DEFAULT '',
    payload    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_by BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (act_year, number)
);

-- Какой акт составлен по заявке. Акт может покрывать несколько заявок одной
-- поставки; заявка входит не больше чем в один акт. Удалённый акт заявку не
-- удаляет — она просто снова без акта.
ALTER TABLE claim ADD COLUMN IF NOT EXISTS act_id BIGINT REFERENCES torg_act(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS claim_act ON claim(act_id) WHERE act_id IS NOT NULL;

-- Общие настройки раздела. Пока одна: реквизиты организации и комиссия для
-- бланка. Общие, а не личные: акт должен выходить одинаково у кого бы ни был
-- составлен, а реквизиты правит один человек, а не каждый на своём компьютере.
-- Заполняются в приложении (Торг-2 → Ещё → Реквизиты ТОРГ-2); в миграции их нет:
-- репозиторий публичный, а реквизиты — данные конкретной организации.
CREATE TABLE IF NOT EXISTS claim_setting (
    key        TEXT PRIMARY KEY,
    value      JSONB       NOT NULL,
    updated_by BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
