-- Смена пароля при первом входе и защита от подбора.
--
-- Пароли раздавались списком, и человек, получивший свой, обязан заменить его
-- на известный только ему: иначе тот, кто видел список, входит под чужим именем
-- и правит чужие оплаты, а в журнале остаётся не он.
--
-- Отдельный файл, а не правка schema.sql: том с базой уже не пустой, и
-- docker-entrypoint-initdb.d на нём больше не срабатывает.

ALTER TABLE app_user
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;

-- Неудачные попытки входа. Пишутся по логину, а не по учётке: перебирать
-- будут и несуществующие имена, и их тоже нужно придерживать.
CREATE TABLE IF NOT EXISTS login_attempt (
    id      BIGSERIAL PRIMARY KEY,
    login   TEXT        NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    success BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS login_attempt_recent ON login_attempt(login, at DESC);

-- Всем, кто заведён раздачей паролей, пароль сменить обязательно.
-- Администратор исключён: он свой пароль и назначал.
UPDATE app_user SET must_change_password = TRUE WHERE NOT is_admin;
