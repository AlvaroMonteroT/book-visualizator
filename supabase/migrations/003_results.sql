-- Persistent character evidence produced by the analysis step.
create table if not exists public.character_results (
  result_id text primary key,
  book_id text not null references public.books(book_id) on delete cascade,
  character text not null,
  description jsonb not null default '{}'::jsonb,
  quotes jsonb not null default '[]'::jsonb,
  image_path text,
  created_at timestamptz not null default now()
);

alter table public.questions add column if not exists result_id text unique;

create index if not exists character_results_book_idx
  on public.character_results (book_id, character);

alter table public.character_results enable row level security;
create policy "public can read character results"
  on public.character_results for select using (true);

grant select on public.character_results to anon, authenticated;
revoke insert, update, delete on public.character_results from anon, authenticated;
