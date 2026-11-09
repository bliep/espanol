-- Phase 5: running cost per session (agent/costs.py CostBreakdown, USD).
alter table sessions add column cost jsonb;
