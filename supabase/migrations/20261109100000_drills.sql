-- Drills (Phase 4): one row per answered drill item, plus a score per word in it.
-- Translation drills score the target word; repeat-after-me scores every word (Azure).

create table drill_attempts (
  id         bigint generated always as identity primary key,
  learner_id uuid not null references learners (id) on delete cascade,
  session_id uuid references sessions (id) on delete set null,
  mode       text not null,  -- modes.LessonMode
  prompt     text not null,  -- what the learner was given
  expected   text not null,  -- model answer / reference sentence
  answer     text not null,  -- what the learner said or typed
  score      real not null,  -- 0–100
  details    jsonb not null, -- translation verdict or pronunciation result
  created_at timestamptz not null default now()
);
create index drill_attempts_learner_created on drill_attempts (learner_id, created_at desc);

create table word_scores (
  id         bigint generated always as identity primary key,
  learner_id uuid not null references learners (id) on delete cascade,
  attempt_id bigint not null references drill_attempts (id) on delete cascade,
  term       text not null,  -- match key, as vocab.term
  word       text not null,  -- display
  mode       text not null,
  score      real not null,  -- 0–100
  created_at timestamptz not null default now()
);
create index word_scores_learner_term on word_scores (learner_id, term, created_at desc);

alter table drill_attempts enable row level security;
alter table word_scores    enable row level security;

create policy "own drill attempts" on drill_attempts for select using (auth.uid() = learner_id);
create policy "own word scores"    on word_scores    for select using (auth.uid() = learner_id);
