-- Торг-2: недопоставки и браки.
--
-- До этой миграции заявки велись в общей таблице Google: склад вписывал строку,
-- категорийный менеджер (КМ) ставил себе ответ, закрытые уезжали на лист
-- «Архив». Здесь то же самое, но с учётками: кто заявил, кому адресовано, кто
-- закрыл и чем кончилось — видно из базы, а не из памяти.
--
-- Архив — не отдельная таблица, а заявки со статусом «closed». Отдельная
-- таблица заставила бы переносить строки туда и обратно, а вернуть ошибочно
-- закрытую заявку — это именно перенос назад, с потерей вложений и журнала.

CREATE TABLE IF NOT EXISTS claim (
    id           BIGSERIAL PRIMARY KEY,
    -- Недопоставка или брак. В таблице вида не было, и его приходилось
    -- угадывать по комментарию.
    kind         TEXT        NOT NULL CHECK (kind IN ('shortage', 'defect')),
    brand        TEXT        NOT NULL DEFAULT '',
    product      TEXT        NOT NULL DEFAULT '',
    -- Характеристика: объём, оттенок — то, чем один товар карточки отличается
    -- от другого.
    spec         TEXT        NOT NULL DEFAULT '',
    barcode      TEXT        NOT NULL DEFAULT '',
    quantity     NUMERIC(12, 2) NOT NULL DEFAULT 1,
    -- Срок годности. Пусто — у товара его нет или склад его не указал.
    expiry_date  DATE,
    comment      TEXT        NOT NULL DEFAULT '',
    -- new — склад завёл, КМ ещё не ответил; accepted — «Принято в работу»;
    -- closed — «Закрыто», заявка в архиве.
    status       TEXT        NOT NULL DEFAULT 'new'
                             CHECK (status IN ('new', 'accepted', 'closed')),
    applicant_id BIGINT      NOT NULL REFERENCES app_user(id),
    -- КМ может быть не назначен: склад не всегда знает, чей это бренд, и
    -- назначить его потом должен мочь любой. Заявка без КМ видна всем.
    manager_id   BIGINT      REFERENCES app_user(id),
    -- Видел ли КМ, что заявку поставили на него. Падает в FALSE, когда
    -- менеджера назначают или меняют (кроме случая, когда человек назначил
    -- сам себя), и возвращается, когда он открыл заявку. По этой отметке
    -- приложение показывает уведомление и цифру у раздела; хранится в базе,
    -- а не в приложении, чтобы перезапуск не терял непрочитанное и не
    -- показывал одно и то же дважды.
    manager_seen BOOLEAN     NOT NULL DEFAULT TRUE,
    -- Чем кончилось. Заполняется при закрытии — по нему потом можно считать,
    -- сколько вернули деньгами, а сколько просто довезли.
    resolution   TEXT        NOT NULL DEFAULT '',
    close_note   TEXT        NOT NULL DEFAULT '',
    closed_at    TIMESTAMPTZ,
    closed_by    BIGINT      REFERENCES app_user(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Открытые заявки читают чаще и всегда целиком; закрытых больше с каждым
-- месяцем, и список не должен их перебирать.
CREATE INDEX IF NOT EXISTS claim_open ON claim(created_at DESC) WHERE status <> 'closed';
CREATE INDEX IF NOT EXISTS claim_closed ON claim(closed_at DESC) WHERE status = 'closed';
-- Уведомления: сколько непрочитанного у менеджера — запрос раз в минуту от
-- каждого запущенного приложения.
CREATE INDEX IF NOT EXISTS claim_unseen ON claim(manager_id)
    WHERE status <> 'closed' AND NOT manager_seen;
-- Подсказка по штрих-коду при заведении: тот же товар уже заводили.
CREATE INDEX IF NOT EXISTS claim_barcode ON claim(barcode) WHERE barcode <> '';

-- ТОРГ-2 и фотографии брака. Файлы лежат в томе сервера, в базе — только имя,
-- как у вложений оплат.
CREATE TABLE IF NOT EXISTS claim_file (
    id        BIGSERIAL PRIMARY KEY,
    claim_id  BIGINT      NOT NULL REFERENCES claim(id) ON DELETE CASCADE,
    name      TEXT        NOT NULL,
    stored_as TEXT        NOT NULL,
    size      BIGINT      NOT NULL DEFAULT 0,
    added_by  BIGINT      REFERENCES app_user(id),
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS claim_file_claim ON claim_file(claim_id);
