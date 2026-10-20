-- Личный предел обращений к ИИ-правке.
--
-- Общий предел (AI_DAILY_LIMIT) подходит не всем: одному менеджеру правка нужна
-- каждый день, другому не нужна совсем. Администратор задаёт предел в учётной
-- записи. NULL — «как у всех», то есть общий предел сервера; 0 — ИИ человеку
-- закрыт.

ALTER TABLE app_user
    ADD COLUMN IF NOT EXISTS ai_daily_limit INTEGER
    CHECK (ai_daily_limit IS NULL OR ai_daily_limit BETWEEN 0 AND 1000);
