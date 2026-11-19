-- personal-agent M0 schema. 可直接执行：psql "$DATABASE_URL" -f migrations/0001_init.sql
-- 也可以用 `personal-agent migrate`（会记录到 schema_migrations）。语句均可重复执行。

CREATE TABLE IF NOT EXISTS jobs (
  job_id           uuid PRIMARY KEY,
  task_type        text        NOT NULL,
  pool             text        NOT NULL DEFAULT 'default',
  user_id          text        NOT NULL DEFAULT 'local',
  payload_ref      jsonb       NOT NULL,            -- {uri, sha256, size}，内容在 Artifact Store
  priority         smallint    NOT NULL DEFAULT 5,  -- 数字越小越优先
  idempotency_key  text        NOT NULL UNIQUE,
  trace_id         text        NOT NULL,
  deadline_at      timestamptz,
  parent_job_id    uuid        REFERENCES jobs(job_id),
  state            text        NOT NULL DEFAULT 'queued'
                   CHECK (state IN ('queued','retry_wait','leased','succeeded','dead','cancelled')),
  attempt          int         NOT NULL DEFAULT 0,  -- 每次 lease +1，同时作为 fencing token
  max_attempts     int         NOT NULL DEFAULT 5,
  run_after        timestamptz NOT NULL DEFAULT now(),
  lease_owner      text,
  lease_until      timestamptz,
  result_ref       jsonb,
  last_error       jsonb,
  enqueued_at      timestamptz NOT NULL DEFAULT now(),
  leased_at        timestamptz,
  finished_at      timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (pool, priority, enqueued_at)
  WHERE state IN ('queued','retry_wait');
CREATE INDEX IF NOT EXISTS jobs_leased ON jobs (lease_until) WHERE state = 'leased';
CREATE INDEX IF NOT EXISTS jobs_parent ON jobs (parent_job_id) WHERE parent_job_id IS NOT NULL;

-- 每次 lease 一行。outcome: running → persisted → acked；或 fenced / lease_expired / failed
CREATE TABLE IF NOT EXISTS attempts (
  job_id       uuid        NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  attempt      int         NOT NULL,
  worker       text        NOT NULL,
  started_at   timestamptz NOT NULL DEFAULT now(),
  persisted_at timestamptz,
  finished_at  timestamptz,
  outcome      text        NOT NULL DEFAULT 'running',
  result_ref   jsonb,                               -- durable receipt：结果产物引用（ACK 前写入）
  error        jsonb,
  PRIMARY KEY (job_id, attempt)
);
CREATE INDEX IF NOT EXISTS attempts_receipt ON attempts (job_id) WHERE outcome = 'persisted';

-- 长任务分步检查点（JobContext.step）
CREATE TABLE IF NOT EXISTS checkpoints (
  job_id      uuid        NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  step        text        NOT NULL,
  input_hash  text        NOT NULL,
  output_ref  jsonb       NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, step)
);

CREATE TABLE IF NOT EXISTS dead_letters (
  job_id    uuid        PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
  reason    text        NOT NULL,
  error     jsonb,
  attempts  int         NOT NULL,
  dead_at   timestamptz NOT NULL DEFAULT now()
);

-- 对外副作用账本（JobContext.effect）：同一 effect_key 只执行一次
CREATE TABLE IF NOT EXISTS effects (
  effect_key  text        PRIMARY KEY,
  job_id      uuid        NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
  state       text        NOT NULL CHECK (state IN ('started','done')),
  receipt     jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  done_at     timestamptz
);
