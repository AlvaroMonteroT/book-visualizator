-- Shared-library access rules.
-- Public visitors may read approved results; only the trusted backend writes.

-- Keep the original book text and processing internals behind the backend.
revoke all on table public.book_paragraphs from anon, authenticated;
revoke all on table public.jobs from anon, authenticated;

-- Shared catalog/results are readable without user accounts.
grant select on table public.books, public.characters, public.images, public.questions
  to anon, authenticated;

-- No browser client may create, edit, or delete shared records.
revoke insert, update, delete on table public.books, public.characters, public.images, public.questions
  from anon, authenticated;

drop policy if exists "Public can read available books" on public.books;
create policy "Public can read available books"
  on public.books for select
  to anon, authenticated
  using (status in ('uploaded', 'indexing', 'ready'));

drop policy if exists "Public can read characters" on public.characters;
create policy "Public can read characters"
  on public.characters for select
  to anon, authenticated
  using (true);

drop policy if exists "Public can read completed images" on public.images;
create policy "Public can read completed images"
  on public.images for select
  to anon, authenticated
  using (status = 'completed');

drop policy if exists "Public can read book questions" on public.questions;
create policy "Public can read book questions"
  on public.questions for select
  to anon, authenticated
  using (true);

-- Storage rules for generated images. The bucket is public for downloads,
-- but browser clients cannot upload, replace, or delete objects.
drop policy if exists "Backend can manage generated images" on storage.objects;
create policy "Backend can manage generated images"
  on storage.objects for all
  to service_role
  using (bucket_id = 'generated-images')
  with check (bucket_id = 'generated-images');

-- No public storage policy is created for book-files: it remains private.
-- The backend service role can access it without exposing EPUBs to visitors.
