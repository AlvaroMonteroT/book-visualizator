-- Shared Book Visualizator library.
-- All writes are performed by the trusted backend service.

create table if not exists public.books (
  book_id text primary key,
  title text not null,
  source_filename text,
  source_sha256 text not null unique,
  source_storage_path text,
  status text not null default 'uploaded'
    check (status in ('uploaded', 'indexing', 'ready', 'failed')),
  paragraph_count integer not null default 0,
  chapter_count integer not null default 0,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.book_paragraphs (
  book_id text not null references public.books(book_id) on delete cascade,
  chapter_number integer not null,
  paragraph_number integer not null,
  paragraph_text text not null,
  primary key (book_id, chapter_number, paragraph_number)
);

create table if not exists public.characters (
  character_id bigint generated always as identity primary key,
  book_id text not null references public.books(book_id) on delete cascade,
  name text not null,
  aliases jsonb not null default '[]'::jsonb,
  evidence_count integer not null default 0,
  description text,
  image_path text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (book_id, name)
);

create table if not exists public.images (
  image_id bigint generated always as identity primary key,
  book_id text not null references public.books(book_id) on delete cascade,
  character_id bigint references public.characters(character_id) on delete set null,
  image_type text not null check (image_type in ('portrait', 'scene')),
  storage_path text not null,
  prompt_hash text,
  status text not null default 'completed'
    check (status in ('queued', 'generating', 'completed', 'failed')),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (book_id, image_type, prompt_hash)
);

create table if not exists public.jobs (
  job_id text primary key,
  book_id text references public.books(book_id) on delete cascade,
  job_type text not null check (job_type in ('index', 'character', 'scene', 'question')),
  status text not null default 'queued'
    check (status in ('queued', 'running', 'completed', 'failed')),
  progress integer not null default 0 check (progress between 0 and 100),
  message text,
  result_id text,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.questions (
  question_id bigint generated always as identity primary key,
  book_id text not null references public.books(book_id) on delete cascade,
  question text not null,
  answer text,
  citations jsonb not null default '[]'::jsonb,
  context jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists book_paragraphs_book_idx
  on public.book_paragraphs (book_id, chapter_number, paragraph_number);
create index if not exists characters_book_idx
  on public.characters (book_id);
create index if not exists images_book_idx
  on public.images (book_id, image_type);
create index if not exists jobs_book_idx
  on public.jobs (book_id, status);
create index if not exists questions_book_idx
  on public.questions (book_id, created_at desc);

-- Keep the database protected even though this is a shared public library.
-- The backend will use the Supabase service key and perform all writes.
alter table public.books enable row level security;
alter table public.book_paragraphs enable row level security;
alter table public.characters enable row level security;
alter table public.images enable row level security;
alter table public.jobs enable row level security;
alter table public.questions enable row level security;

-- Shared data is read through FastAPI. No anonymous write access is granted.OK 
