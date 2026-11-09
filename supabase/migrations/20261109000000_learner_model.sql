-- Learner model: profile, vocab (FSRS cards), error log, sessions.
-- The agent writes via the Postgres service connection (bypasses RLS);
-- RLS only lets a logged-in learner read their own rows.

create table learners (
  id         uuid primary key references auth.users (id) on delete cascade,
  level      text not null check (level in ('A1', 'A2', 'B1', 'B2', 'C1')),
  created_at timestamptz not null default now()
);

create table sessions (
  id         uuid primary key default gen_random_uuid(),
  learner_id uuid not null references learners (id) on delete cascade,
  level      text not null,
  started_at timestamptz not null default now(),
  ended_at   timestamptz,
  summary    jsonb
);
create index sessions_learner_started on sessions (learner_id, started_at desc);

-- One FSRS card per word. term = match key ("paraguas"); spanish = display ("el paraguas").
create table vocab (
  learner_id uuid not null references learners (id) on delete cascade,
  term       text not null,
  spanish    text not null,
  english    text not null,
  card       jsonb not null,  -- fsrs.Card.to_dict()
  due        timestamptz not null,
  created_at timestamptz not null default now(),
  primary key (learner_id, term)
);
create index vocab_learner_due on vocab (learner_id, due);

create table errors (
  id         bigint generated always as identity primary key,
  learner_id uuid not null references learners (id) on delete cascade,
  session_id uuid references sessions (id) on delete set null,
  category   text not null,
  original   text not null,
  corrected  text not null,
  rule       text not null,
  created_at timestamptz not null default now()
);
create index errors_learner_created on errors (learner_id, created_at desc);

alter table learners enable row level security;
alter table sessions enable row level security;
alter table vocab    enable row level security;
alter table errors   enable row level security;

create policy "own profile"  on learners for select using (auth.uid() = id);
create policy "own sessions" on sessions for select using (auth.uid() = learner_id);
create policy "own vocab"    on vocab    for select using (auth.uid() = learner_id);
create policy "own errors"   on errors   for select using (auth.uid() = learner_id);
