-- Схема базы оплат на сервере.
--
-- Повторяет таблицы локальной SQLite-базы (app/core/payments/schema.py), но с
-- тремя осознанными отличиями — они возможны только на сервере и здесь уместны:
--
--   1. Деньги в NUMERIC, а не в REAL. Локально суммы складывались во float, и на
--      2.4 млрд рублей это давало расхождение в копейках при каждом пересчёте
--      аналитики. NUMERIC(16, 2) считает точно.
--   2. Даты в DATE, а пустая дата — NULL вместо ''. В SQLite пустая строка была
--      единственным способом сказать «даты нет», из-за чего каждый отбор нёс
--      условие `pay_date <> ''`. Здесь это выражается типом.
--   3. Появились учётные записи и журнал изменений: база стала общей, и теперь
--      нужно знать, кто именно правил запись.
--
-- Имена столбцов платежа намеренно оставлены прежними: клиент отображает их в
-- тот же dataclass Payment, и расхождение в названиях ничего бы не дало.

-- --- учётные записи ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_user (
    id            BIGSERIAL PRIMARY KEY,
    login         TEXT        NOT NULL UNIQUE,
    full_name     TEXT        NOT NULL DEFAULT '',
    password_hash TEXT        NOT NULL,
    is_admin      BOOLEAN     NOT NULL DEFAULT FALSE,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Связь учётки с тем, как человек записан в выгрузке 1С.
--
-- Отдельная таблица, а не столбец: один и тот же менеджер приходит из 1С под
-- разными написаниями — где-то «Базина Виктория», где-то с отчеством. На 6958
-- строках 57 различных значений `responsible` примерно на полтора десятка
-- живых людей. Право на правку считается по вхождению в этот список.
CREATE TABLE IF NOT EXISTS user_responsible (
    user_id     BIGINT NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    responsible TEXT   NOT NULL,
    PRIMARY KEY (user_id, responsible)
);

CREATE INDEX IF NOT EXISTS user_responsible_name ON user_responsible(responsible);

-- --- оплаты -----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS payment (
    id            BIGSERIAL PRIMARY KEY,
    doc_number    TEXT          NOT NULL DEFAULT '',
    request_date  DATE,
    pay_date      DATE,
    amount        NUMERIC(16,2) NOT NULL DEFAULT 0,
    vat           NUMERIC(16,2) NOT NULL DEFAULT 0,
    currency      TEXT          NOT NULL DEFAULT 'руб.',
    supplier_id   BIGINT        NOT NULL DEFAULT 0,
    recipient     TEXT          NOT NULL DEFAULT '',
    recipient_key TEXT          NOT NULL DEFAULT '',
    status        TEXT          NOT NULL DEFAULT 'planned',
    source_status TEXT          NOT NULL DEFAULT '',
    paid_flag     BOOLEAN       NOT NULL DEFAULT FALSE,
    operation     TEXT          NOT NULL DEFAULT '',
    over_limit    BOOLEAN       NOT NULL DEFAULT FALSE,
    priority      TEXT          NOT NULL DEFAULT '',
    edo_state     TEXT          NOT NULL DEFAULT '',
    responsible   TEXT          NOT NULL DEFAULT '',
    author        TEXT          NOT NULL DEFAULT '',
    comment       TEXT          NOT NULL DEFAULT '',
    had_files     BOOLEAN       NOT NULL DEFAULT FALSE,
    origin        TEXT          NOT NULL DEFAULT 'manual',
    origin_ref    TEXT          NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    -- Кто последним менял запись. NULL — запись пришла из импорта 1С и руками
    -- её не трогали; в этом случае показывать нужно `author` из выгрузки.
    updated_by    BIGINT        REFERENCES app_user(id) ON DELETE SET NULL
);

-- Номер заявки 1С обнуляется каждый год: IP00-000001 встречается и в 2022,
-- и в 2026. Уникальна пара с датой заявки. Ключ частичный: у 28 записей,
-- созданных руками, номера нет, и пустые пары не должны конфликтовать.
CREATE UNIQUE INDEX IF NOT EXISTS payment_doc
    ON payment(doc_number, request_date)
    WHERE doc_number <> '';

CREATE INDEX IF NOT EXISTS payment_pay_date ON payment(pay_date);
CREATE INDEX IF NOT EXISTS payment_status ON payment(status);
CREATE INDEX IF NOT EXISTS payment_supplier ON payment(supplier_id, pay_date);
CREATE INDEX IF NOT EXISTS payment_recipient ON payment(recipient_key, pay_date);
CREATE INDEX IF NOT EXISTS payment_responsible ON payment(responsible);

CREATE TABLE IF NOT EXISTS payment_file (
    id         BIGSERIAL PRIMARY KEY,
    payment_id BIGINT      NOT NULL REFERENCES payment(id) ON DELETE CASCADE,
    name       TEXT        NOT NULL DEFAULT '',
    -- Имя файла в хранилище сервера, а не путь на диске автора: локальный
    -- «C:\Users\...» другим менеджерам ничего не открывает.
    stored_as  TEXT        NOT NULL DEFAULT '',
    size       BIGINT      NOT NULL DEFAULT 0,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by   BIGINT      REFERENCES app_user(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS payment_file_owner ON payment_file(payment_id);

CREATE TABLE IF NOT EXISTS budget (
    year       INTEGER       NOT NULL,
    month      INTEGER       NOT NULL,
    amount     NUMERIC(16,2) NOT NULL DEFAULT 0,
    note       TEXT          NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_by BIGINT        REFERENCES app_user(id) ON DELETE SET NULL,
    PRIMARY KEY (year, month)
);

-- «Получатель» в 1С — это юрлицо («НеваЛайн ООО»), а карточка поставщика
-- заведена под торговым именем. Соответствие ищется по нормализованному имени
-- и запоминается здесь, чтобы автоподбор не повторялся на каждом чтении.
CREATE TABLE IF NOT EXISTS recipient_link (
    recipient_key TEXT        PRIMARY KEY,
    recipient     TEXT        NOT NULL DEFAULT '',
    supplier_id   BIGINT      NOT NULL DEFAULT 0,
    linked_by     TEXT        NOT NULL DEFAULT 'auto',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recipient_link_supplier ON recipient_link(supplier_id);

CREATE TABLE IF NOT EXISTS import_run (
    id           BIGSERIAL PRIMARY KEY,
    path         TEXT        NOT NULL DEFAULT '',
    file_hash    TEXT        NOT NULL DEFAULT '',
    rows_total   INTEGER     NOT NULL DEFAULT 0,
    rows_new     INTEGER     NOT NULL DEFAULT 0,
    rows_updated INTEGER     NOT NULL DEFAULT 0,
    rows_same    INTEGER     NOT NULL DEFAULT 0,
    rows_skipped INTEGER     NOT NULL DEFAULT 0,
    error        TEXT        NOT NULL DEFAULT '',
    finished_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_by   BIGINT      REFERENCES app_user(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS import_run_hash ON import_run(file_hash);

-- --- журнал изменений -------------------------------------------------------

-- База общая, и «кто перенёс дату оплаты» перестало быть праздным вопросом.
-- Пишется на каждое изменение, сделанное руками; массовый импорт из 1С даёт
-- одну запись на прогон, а не на строку.
CREATE TABLE IF NOT EXISTS audit_log (
    id        BIGSERIAL PRIMARY KEY,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id   BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    entity    TEXT        NOT NULL,
    entity_id BIGINT      NOT NULL DEFAULT 0,
    action    TEXT        NOT NULL,
    -- Прежнее и новое значение только изменённых полей: хранить снимок целиком
    -- на каждое касание комментария — это гигабайты ради ничего.
    changes   JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS audit_log_entity ON audit_log(entity, entity_id, at DESC);
CREATE INDEX IF NOT EXISTS audit_log_user ON audit_log(user_id, at DESC);
