-- Learner model (Azure SQL): profile, vocab (FSRS cards), error log, sessions, drills.
-- Only the agent reads and writes (login tutor_agent); the browser never touches the database.
-- Deleting a learner removes all their rows. SQL Server allows one cascade path per row, so
-- session/attempt links on child rows don't cascade; the learner cascade removes those rows.

create table learners (
  id         uniqueidentifier primary key,  -- Entra External ID object id (LiveKit identity)
  level      varchar(2) not null check (level in ('A1', 'A2', 'B1', 'B2', 'C1')),
  created_at datetimeoffset not null default sysdatetimeoffset()
);

create table sessions (
  id         uniqueidentifier primary key default newid(),
  learner_id uniqueidentifier not null references learners (id) on delete cascade,
  level      varchar(2) not null,
  started_at datetimeoffset not null default sysdatetimeoffset(),
  ended_at   datetimeoffset,
  summary    nvarchar(max) check (isjson(summary) = 1),  -- SessionSummary
  cost       nvarchar(max) check (isjson(cost) = 1)      -- costs.CostBreakdown, USD
);
create index sessions_learner_started on sessions (learner_id, started_at desc);

-- One FSRS card per word. term = match key ("paraguas"); spanish = display ("el paraguas").
create table vocab (
  learner_id uniqueidentifier not null references learners (id) on delete cascade,
  term       nvarchar(200) not null,
  spanish    nvarchar(400) not null,
  english    nvarchar(400) not null,
  card       nvarchar(max) not null check (isjson(card) = 1),  -- fsrs.Card.to_dict()
  due        datetimeoffset not null,
  created_at datetimeoffset not null default sysdatetimeoffset(),
  primary key (learner_id, term)
);
create index vocab_learner_due on vocab (learner_id, due);

create table errors (
  id         bigint identity primary key,
  learner_id uniqueidentifier not null references learners (id) on delete cascade,
  session_id uniqueidentifier references sessions (id),
  category   varchar(40) not null,
  original   nvarchar(max) not null,
  corrected  nvarchar(max) not null,
  [rule]     nvarchar(max) not null,  -- RULE is a T-SQL keyword
  created_at datetimeoffset not null default sysdatetimeoffset()
);
create index errors_learner_created on errors (learner_id, created_at desc);

-- One row per answered drill item, plus a score per word in it.
-- Translation drills score the target word; repeat-after-me scores every word (Azure).
create table drill_attempts (
  id         bigint identity primary key,
  learner_id uniqueidentifier not null references learners (id) on delete cascade,
  session_id uniqueidentifier references sessions (id),
  mode       varchar(40) not null,      -- modes.LessonMode
  prompt     nvarchar(max) not null,    -- what the learner was given
  expected   nvarchar(max) not null,    -- model answer / reference sentence
  answer     nvarchar(max) not null,    -- what the learner said or typed
  score      real not null,             -- 0–100
  details    nvarchar(max) not null check (isjson(details) = 1),
  created_at datetimeoffset not null default sysdatetimeoffset()
);
create index drill_attempts_learner_created on drill_attempts (learner_id, created_at desc);

create table word_scores (
  id         bigint identity primary key,
  learner_id uniqueidentifier not null references learners (id),
  attempt_id bigint not null references drill_attempts (id) on delete cascade,
  term       nvarchar(200) not null,  -- match key, as vocab.term
  word       nvarchar(400) not null,  -- display
  mode       varchar(40) not null,
  score      real not null,           -- 0–100
  created_at datetimeoffset not null default sysdatetimeoffset()
);
create index word_scores_learner_term on word_scores (learner_id, term, created_at desc);
