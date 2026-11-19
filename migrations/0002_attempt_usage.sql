-- M0.2：每次 attempt 的模型用量（token 数）。成功和失败的 attempt 都会写。
-- 格式：[{"backend", "model", "usage": {"requests", "input_tokens", "output_tokens", "total_tokens"}}, ...]
ALTER TABLE attempts ADD COLUMN IF NOT EXISTS usage jsonb;
