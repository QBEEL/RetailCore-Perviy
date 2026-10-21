-- Замещение на время отпуска.
--
-- Правка оплаты разрешена тому, чьё имя из выгрузки 1С стоит в `responsible`
-- (таблица user_responsible). Когда человек уходит в отпуск, его оплаты
-- должен вести кто-то другой, а переписывать для этого закрепление имён и
-- потом возвращать всё назад — верный способ потерять чужие оплаты.
--
-- Замещение — это отдельная запись «кто, кого и на какой срок». Пока срок
-- идёт, заместитель правит оплаты отсутствующего так же, как свои; закончился —
-- право исчезает само, ничего возвращать не нужно. Имена в user_responsible
-- не трогаются, поэтому у отсутствующего ничего не отнимается, а оплаты
-- остаются записанными на него: в таблице видно, чьи они на самом деле.
--
-- Даты включительно, сутки считаются по Москве — по ним люди и работают.

CREATE TABLE IF NOT EXISTS user_cover (
    id         BIGSERIAL PRIMARY KEY,
    absent_id  BIGINT      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    cover_id   BIGINT      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    date_from  DATE        NOT NULL,
    date_to    DATE        NOT NULL,
    note       TEXT        NOT NULL DEFAULT '',
    created_by BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (absent_id <> cover_id),
    CHECK (date_to >= date_from)
);

CREATE INDEX IF NOT EXISTS user_cover_cover ON user_cover(cover_id, date_from, date_to);
