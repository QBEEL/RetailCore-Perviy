-- Счётчик обращений к ИИ-правке заметок.
--
-- Запросы к Gemini идут с общего ключа организации, а он платный: без счёта один
-- человек, зациклившийся на кнопке, исчерпал бы квоту за всех. Поэтому на
-- каждого пользователя за день считаются обращения (их предел задаёт сервер,
-- AI_DAILY_LIMIT) и объём отправленного текста.
--
-- Сам текст заметок не хранится нигде: ни здесь, ни в журнале изменений.
-- Сутки считаются по Москве — по ним люди и работают.

CREATE TABLE IF NOT EXISTS ai_usage (
    user_id  BIGINT  NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    day      DATE    NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    chars    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
