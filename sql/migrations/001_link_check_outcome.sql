-- Classify URL checks so bot-blocking (403) is not confused with dead links (404).
ALTER TABLE source_link_checks ADD COLUMN IF NOT EXISTS outcome TEXT;
ALTER TABLE state_source_registry ADD COLUMN IF NOT EXISTS urls_reachable_at TIMESTAMPTZ;
COMMENT ON COLUMN source_link_checks.outcome IS
  'ok | blocked_automated (401/403/429: server up, refuses scripts) | not_found (404/410) | server_error | tls_error | timeout | connection_error';
