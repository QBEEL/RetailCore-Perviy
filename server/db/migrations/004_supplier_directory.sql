-- Направления и закрепление поставщиков за менеджерами.
--
-- До этой миграции «менеджер поставщика» нигде не хранился, а вычислялся:
-- группировкой оплат по `responsible` с сортировкой по их количеству. Это
-- отвечало на вопрос «кто чаще платил», а не «кто ведёт», поэтому разовая
-- оплата за коллегу навсегда добавляла человека в список, а поставщик без
-- оплат не показывался у своего менеджера вовсе.
--
-- Здесь закрепление становится фактом, который кто-то заявил и кто-то
-- подтвердил. Вычисляемый список плательщиков остаётся, но уже как справка.

-- --- направления ------------------------------------------------------------

-- Таблица, а не enum: направления будут добавляться, а ALTER TYPE в
-- транзакции с использованием нового значения не работает.
CREATE TABLE IF NOT EXISTS direction (
    id         SMALLSERIAL PRIMARY KEY,
    code       TEXT    NOT NULL UNIQUE,
    title      TEXT    NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO direction (code, title, sort_order) VALUES
    ('beauty',  'Beauty',  1),
    ('fashion', 'Fashion', 2)
ON CONFLICT (code) DO NOTHING;

-- Направление менеджера. Множественное: кто-то может вести оба отдела, и
-- запрет на это пришлось бы снимать миграцией в самый неудобный момент.
CREATE TABLE IF NOT EXISTS user_direction (
    user_id      BIGINT   NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    direction_id SMALLINT NOT NULL REFERENCES direction(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, direction_id)
);

CREATE INDEX IF NOT EXISTS user_direction_dir ON user_direction(direction_id);

-- --- закрепление ------------------------------------------------------------

-- Ключ — `recipient_key`, а не `supplier_id`. Карточка поставщика заведена у
-- единиц: её заводят руками и только ради разбора прайса. Получатель же
-- приходит с каждой выгрузкой 1С, и настоящий список поставщиков — это он.
-- По той же причине здесь нет внешнего ключа: справочника получателей как
-- таблицы не существует, есть только колонка в оплатах.
--
-- Пара (получатель, менеджер), а не колонка «ответственный»: одного поставщика
-- ведут несколько человек по разным брендам. Единственный владелец исказил бы
-- картину у заметной части поставщиков.
CREATE TABLE IF NOT EXISTS supplier_assignment (
    recipient_key TEXT        NOT NULL,
    user_id       BIGINT      NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    -- draft — менеджер заявил и может отозвать сам.
    -- fixed — администратор подтвердил, правит дальше только он.
    state         TEXT        NOT NULL DEFAULT 'draft'
                              CHECK (state IN ('draft', 'fixed')),
    claimed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by    BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    fixed_at      TIMESTAMPTZ,
    fixed_by      BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    PRIMARY KEY (recipient_key, user_id)
);

-- Отбор «мои поставщики» — самый частый запрос вкладки, и он идёт по паре
-- «человек и состояние»: черновик виден сразу после заявки, наравне с
-- зафиксированным.
CREATE INDEX IF NOT EXISTS supplier_assignment_user
    ON supplier_assignment(user_id, state);

-- Очередь администратора: все черновики, ждущие подтверждения.
CREATE INDEX IF NOT EXISTS supplier_assignment_draft
    ON supplier_assignment(claimed_at) WHERE state = 'draft';

-- --- направление поставщика --------------------------------------------------

-- source = 'auto'   — проставлено по направлениям закреплённых менеджеров;
--          'manual' — задано администратором, автоправилом не перетирается.
--
-- Разделение нужно, чтобы пересчёт после каждой фиксации не отменял ручную
-- правку: поставщик, которого администратор сознательно оставил в одном
-- направлении, иначе возвращался бы в оба при следующем закреплении.
CREATE TABLE IF NOT EXISTS supplier_direction (
    recipient_key TEXT        NOT NULL,
    direction_id  SMALLINT    NOT NULL REFERENCES direction(id) ON DELETE CASCADE,
    source        TEXT        NOT NULL DEFAULT 'auto'
                              CHECK (source IN ('auto', 'manual')),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    PRIMARY KEY (recipient_key, direction_id)
);

CREATE INDEX IF NOT EXISTS supplier_direction_dir
    ON supplier_direction(direction_id);

-- --- журнал ------------------------------------------------------------------

-- Отдельный журнал, а не общий audit_log: там запись привязана к числовому
-- `entity_id`, а закрепление опознаётся строковым ключом получателя и парой с
-- менеджером. Разбирательство «кто снял с меня поставщика» без журнала
-- упирается в слово против слова, а база теперь общая.
CREATE TABLE IF NOT EXISTS supplier_assignment_log (
    id            BIGSERIAL PRIMARY KEY,
    recipient_key TEXT        NOT NULL,
    user_id       BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    action        TEXT        NOT NULL,
    payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    actor_id      BIGINT      REFERENCES app_user(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS supplier_assignment_log_key
    ON supplier_assignment_log(recipient_key, created_at DESC);
