# Book Visualizator

Book Visualizator is a local web app for uploading an EPUB, finding a
character's physical description, generating a portrait, and opening the
verified source quotes when needed. The existing command-line scripts remain
available; the FastAPI server now connects them to the browser interface.

## Run the web app

Install the dependencies, make sure `OPENAI_API_KEY` is present in `.env`, and
start the local server:

```sh
source .venv/bin/activate
python -m uvicorn backend.app.main:app --reload
```

Then open <http://127.0.0.1:8000>. Upload an `.epub`, enter a character, and
wait for the portrait and description. Source quotes load when you click
`Show source quotes`.

Once the character index is ready, the character field becomes a list of
characters found in the book. Selecting a character starts the description and
portrait workflow immediately.

After a book is uploaded, you can also choose `Visualize a photographed
passage`. Upload a clear JPG, PNG, or WEBP photo of a paragraph. The app reads
the visible words, matches them to the already parsed book, gathers nearby
paragraphs for context, and creates a scene image. The parsed book remains the
source of truth; the photo is only used to identify the passage.

The third mode, `Ask the book`, lets you ask a question in a chat-like box.
The app retrieves relevant paragraphs and sends only those excerpts to the
configured GPT-5.6 Luna analysis model. The answer includes the chapter and
paragraph references used to support it. If a future workflow needs a visual,
the image-generation model remains the configured `gpt-image-2` model.

The browser workflow stores uploaded books, parsed text, results, and images
under `data/`. These files are private local data and are excluded from Git.

When a book is uploaded, the server also builds a reusable character index in
the background. It reads the book sections once, saves character names and
appearance evidence, and uses that index for later character requests. The
index is stored next to the parsed book as `character_index.json` and
`character_evidence.jsonl`.

The first step of this local proof of concept parses an EPUB and saves its text
as numbered paragraphs. Later steps can use those paragraph references to
retrieve exact quotes from the book.

## Parse a book

From the project folder, run:

```sh
python3 scripts/parse_book.py /path/to/book.epub
```

Parsed files are saved under `data/books/<book-id>/`. Choose another output
folder with `--output-dir`:

```sh
python3 scripts/parse_book.py /path/to/book.epub --output-dir data/books
```

The book ID combines a title slug with a hash of the EPUB contents, so two
different editions of a book get separate folders. The parser numbers each
readable EPUB content document as a chapter in reading order, and paragraph
numbers start at 1 within each one. EPUBs do not guarantee that one content
document equals one printed chapter, so these numbers are stable source
locations; they may not match chapter numbers printed in the book.

Each parsed book contains:

- `metadata.json`: title, book ID, content hash, parser version, and counts.
- `paragraphs.jsonl`: one JSON object per paragraph, with `chapter_number`,
  `paragraph_number`, and the exact normalized paragraph text.

Example paragraph record:

```json
{"chapter_number": 1, "paragraph_number": 1, "text": "The original paragraph text."}
```

The parser uses only the Python standard library. It does not call an AI model
or write to the existing database. EPUBs and parsed book text are private local
data and are excluded from Git.

## Find a character

Install the OpenAI Python SDK, then add your key to the ignored `.env` file
(copy `.env.example` to `.env` first if needed):

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The model name and chunk settings are in `config/models.toml`; character
analysis is pinned to Luna. Then pass the parsed book folder and a character
query that includes the version you want:

```sh
python3 scripts/find_character.py \
  --book-dir data/books/<book-id> \
  --character "Darrow when he was a Red"
```

The script scans the whole book in overlapping chunks, verifies returned
paragraph locations against the local parsed book, and saves `description.json`
and `quotes.json` under `data/results/<book-id>/<character>/<run-id>/`. The
quote text is copied from `paragraphs.jsonl`; it is not taken from the model's
response. If no evidence is found, both files are still saved; the description
file has a `no_evidence` status and the quotes file has an empty quote list.

Requests are sent one at a time. Client-side request and estimated-token limits
in `config/models.toml` pace the scan; temporary rate-limit and server errors
are retried with backoff, honoring `Retry-After` when supplied. The token count
is a local estimate, so the API project remains authoritative. After each
successful chunk, progress is saved under `data/results/.checkpoints/`. If a
run stops, rerun the same command with the same book and character query to
resume completed chunks; changing the book, query, model, or chunk settings
starts a separate scan.

## Build a character index

To build the reusable all-characters index for an already parsed book, run:

```sh
python3 scripts/index_characters.py \
  --book-dir data/books/<book-id>
```

The book is scanned once in bounded sections. The resulting index keeps the
character names and exact source paragraphs so descriptions can be created
later without rereading the entire book for every request.

## Generate a realistic character image

The image step consumes the completed `description.json` from the character
step. Luna remains the text-analysis model; image generation uses the separate
GPT Image model configured in `config/models.toml`, with the same
`OPENAI_API_KEY` connection.

Run it with the description output:

```sh
python3 scripts/generate_image.py \
  --description-file data/results/<book-id>/<character>/<run-id>/description.json
```

Add optional visual direction in a Markdown file:

```sh
python3 scripts/generate_image.py \
  --description-file data/results/<book-id>/<character>/<run-id>/description.json \
  --guidelines prompts/portrait_guidelines.md
```

The default settings request a high-quality 1024x1536 portrait PNG from
`gpt-image-2`. Each run saves the image and an `image.json` file under
`data/images/<book-id>/<character>/<run-id>/`. The metadata records the exact
prompt, model settings, source description, and any revised prompt returned by
the image API.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

## Start in production

Cloud hosts should use the command in `start.sh` (also exposed through the
`Procfile`). It binds FastAPI to `0.0.0.0` and uses the host-provided `PORT`;
locally it defaults to port 8000:

```sh
./start.sh
```

Set the values from `.env.example` in the hosting provider's private
environment settings. Do not upload `.env` or any service-role key to GitHub.
