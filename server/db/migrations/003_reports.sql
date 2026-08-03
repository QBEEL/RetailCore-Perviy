-- Отчётность для поставщиков: формат отчёта и правила объединения магазинов.
--
-- И то, и другое общее для всех категорийных менеджеров. Профиль у каждого
-- поставщика свой, а правила «магазин-источник → магазин-приёмник» одни на
-- отдел: если один менеджер учитывает интернет-магазин за универмагом, а
-- второй нет, сводная аналитика по сети перестаёт сходиться, и найти причину
-- по готовым файлам уже нельзя.
--
-- Отдельный файл, а не правка schema.sql: том с базой уже не пустой, и
-- docker-entrypoint-initdb.d на нём больше не срабатывает.

-- Формат отчёта для одного поставщика. Всё, кроме имени и поставщика, лежит
-- одним документом: набор полей, метрик и фильтров меняется вместе, читается
-- целиком и порознь никогда не запрашивается.
CREATE TABLE IF NOT EXISTS report_profile (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT        NOT NULL,
    supplier    TEXT        NOT NULL DEFAULT '',
    supplier_id BIGINT      NOT NULL DEFAULT 0,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  BIGINT      REFERENCES app_user(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS report_profile_name ON report_profile(lower(btrim(name)));

-- Правило «продажи источника учитывать за приёмником». Источник уникален:
-- отправить один магазин сразу в два — это молча удвоенные продажи, и такую
-- настройку лучше не дать сделать, чем потом искать расхождение в отчёте.
--
-- Ключ нормализован так же, как в приложении: регистр и двойные пробелы в
-- выгрузках гуляют, а «ё» пишется через раз.
CREATE TABLE IF NOT EXISTS store_rule (
    id         BIGSERIAL PRIMARY KEY,
    source_key TEXT        NOT NULL UNIQUE,
    source     TEXT        NOT NULL DEFAULT '',
    target     TEXT        NOT NULL DEFAULT '',
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    comment    TEXT        NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by BIGINT      REFERENCES app_user(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS store_rule_target ON store_rule(lower(btrim(target)));
