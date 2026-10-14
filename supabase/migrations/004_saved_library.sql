-- Durable full results for portraits, scenes, and answers.
create table if not exists public.saved_results (
  result_id text primary key,
  book_id text not null references public.books(book_id) on delete cascade,
  payload jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists saved_results_book_idx on public.saved_results (book_id);
alter table public.saved_results enable row level security;
-- FastAPI reads and writes using the backend service role.
revoke all on table public.saved_results from anon, authenticated;
