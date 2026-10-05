const bookInput = document.querySelector('#book-input');
const uploadZone = document.querySelector('#upload-zone');
const bookStatus = document.querySelector('#book-status');
const characterSelect = document.querySelector('#character-select');
const characterToolbar = document.querySelector('.character-toolbar');
const modeSection = document.querySelector('.mode-section');
const modePicker = document.querySelector('.mode-picker');
const modeSummary = document.querySelector('#mode-summary');
const modeSummaryText = document.querySelector('#mode-summary-text');
const changeMode = document.querySelector('#change-mode');
const modeStepTitle = document.querySelector('#mode-step-title');
const modeStepCopy = document.querySelector('#mode-step-copy');
const bookStepTitle = document.querySelector('#book-step-title');
const bookStepCopy = document.querySelector('#book-step-copy');
const characterMode = document.querySelector('#character-mode');
const sceneMode = document.querySelector('#scene-mode');
const questionMode = document.querySelector('#question-mode');
const sceneInput = document.querySelector('#scene-input');
const sceneUploadZone = document.querySelector('#scene-upload-zone');
const sceneStatus = document.querySelector('#scene-status');
const formError = document.querySelector('#form-error');
const emptyState = document.querySelector('#empty-state');
const processingState = document.querySelector('#processing-state');
const resultCard = document.querySelector('#result-card');
const processingTitle = document.querySelector('#processing-title');
const processingMessage = document.querySelector('#processing-message');
const progressBar = document.querySelector('#progress-bar');
const resultBook = document.querySelector('#result-book');
const resultCharacter = document.querySelector('#result-character');
const resultDescription = document.querySelector('#result-description');
const resultContext = document.querySelector('#result-context');
const characterImage = document.querySelector('#character-image');
const quotesButton = document.querySelector('#quotes-button');
const quotesPanel = document.querySelector('#quotes-panel');
const sceneSource = document.querySelector('#scene-source');
const questionToolbar = document.querySelector('.question-toolbar');
const questionInput = document.querySelector('#question-input');
const questionSubmit = document.querySelector('#question-submit');
const questionResult = document.querySelector('#question-result');
const questionResultBook = document.querySelector('#question-result-book');
const questionResultQuestion = document.querySelector('#question-result-question');
const questionAnswer = document.querySelector('#question-answer');
const questionCitations = document.querySelector('#question-citations');
const emptyTitle = document.querySelector('#empty-title');
const emptyDescription = document.querySelector('#empty-description');

let selectedBook = null;
let currentResultId = null;
let activeMode = 'character';
let modeChosen = false;

const modeLabels = {
  character: 'See a character',
  scene: 'Picture a scene',
  question: 'Ask a question',
};

const show = (element) => element.classList.remove('hidden');
const hide = (element) => element.classList.add('hidden');

function setMode(mode) {
  activeMode = mode;
  const characterActive = mode === 'character';
  characterMode.classList.toggle('active', characterActive);
  sceneMode.classList.toggle('active', mode === 'scene');
  questionMode.classList.toggle('active', mode === 'question');
  currentResultId = null;
  hide(resultCard);
  hide(questionResult);
  hide(processingState);
  show(emptyState);
  characterImage.removeAttribute('src');
  quotesPanel.innerHTML = '';
  hide(quotesPanel);
  if (!selectedBook) {
    hide(modeSection);
    hide(characterToolbar);
    hide(document.querySelector('.scene-toolbar'));
    hide(questionToolbar);
    emptyTitle.textContent = 'Choose a book to begin.';
    emptyDescription.textContent = 'Upload an EPUB and we’ll help you explore it.';
    return;
  }
  show(modeSection);
  modeSection.classList.toggle('mode-chosen', modeChosen);
  if (modeChosen) {
    hide(modePicker);
    show(modeSummary);
    modeSummaryText.textContent = modeLabels[mode];
    modeStepTitle.textContent = 'What would you like to do?';
    modeStepCopy.textContent = 'You can change this anytime.';
  } else {
    show(modePicker);
    hide(modeSummary);
    modeStepTitle.textContent = 'What would you like to do?';
    modeStepCopy.textContent = 'Pick one option below.';
    hide(characterToolbar);
    hide(document.querySelector('.scene-toolbar'));
    hide(questionToolbar);
    emptyTitle.textContent = 'Choose what you would like to do.';
    emptyDescription.textContent = 'Pick one of the three options above.';
    return;
  }
  if (characterActive) {
    show(characterToolbar);
    hide(document.querySelector('.scene-toolbar'));
    hide(questionToolbar);
    emptyTitle.textContent = 'Choose a person to begin.';
    emptyDescription.textContent = 'Pick a name from your book to see a picture.';
  } else if (mode === 'scene') {
    hide(characterToolbar);
    show(document.querySelector('.scene-toolbar'));
    hide(questionToolbar);
    emptyTitle.textContent = 'Choose a paragraph photo.';
    emptyDescription.textContent = 'We’ll use the words in your book to create the scene.';
  } else {
    hide(characterToolbar);
    hide(document.querySelector('.scene-toolbar'));
    show(questionToolbar);
    emptyTitle.textContent = 'Ask anything about your book.';
    emptyDescription.textContent = 'We’ll look through the book and explain what we find.';
  }
}

function setError(message = '') {
  formError.textContent = message;
}

function setBookStatus(book) {
  const indexMessage = book.index_job_id ? 'uploaded · preparing character list' : 'uploaded';
  bookStatus.innerHTML = `<span class="check">✓</span><span><strong>${escapeHtml(book.title)}</strong><small>${escapeHtml(book.filename)} · ${indexMessage}</small></span><button id="replace-book" class="text-button" type="button">Replace</button>`;
  show(bookStatus);
  hide(uploadZone);
  bookStepTitle.textContent = 'Your book is ready';
  bookStepCopy.textContent = 'You can change it anytime.';
  modeChosen = false;
  show(modeSection);
  characterSelect.disabled = Boolean(book.index_job_id);
  sceneInput.disabled = false;
  characterMode.disabled = false;
  sceneMode.disabled = false;
  questionMode.disabled = false;
  questionSubmit.disabled = false;
  setMode(activeMode);
  document.querySelector('#replace-book').addEventListener('click', () => bookInput.click());
  if (book.index_job_id) pollIndexJob(book.index_job_id);
  else loadCharacterOptions();
}

async function pollIndexJob(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const job = await response.json();
  if (job.status === 'completed' || job.status === 'failed') {
    characterSelect.disabled = false;
    const small = bookStatus.querySelector('small');
    if (small) small.textContent = job.status === 'completed' ? 'uploaded · character list ready' : 'uploaded · character list unavailable';
    if (job.status === 'completed') await loadCharacterOptions();
    return;
  }
  window.setTimeout(() => pollIndexJob(jobId).catch(() => {}), 1400);
}

async function loadCharacterOptions() {
  const response = await fetch(`/api/books/${selectedBook.book_id}/characters`);
  const data = await response.json();
  if (!response.ok || data.status !== 'ready') return;
  characterSelect.innerHTML = '<option value="">Choose a character</option>' + (data.characters || []).map((character) => `<option value="${escapeHtml(character.name)}">${escapeHtml(character.name)} · ${character.evidence_count} passages</option>`).join('');
  characterSelect.disabled = false;
}

async function uploadBook(file) {
  setError('');
  if (!file.name.toLowerCase().endsWith('.epub')) {
    setError('Please choose an EPUB file.');
    return;
  }
  bookStatus.innerHTML = '<span class="spinner small"></span><span><strong>Uploading book…</strong><small>Preparing the reading desk</small></span>';
  show(bookStatus);
  const form = new FormData();
  form.append('file', file);
  try {
    const response = await fetch('/api/books', { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'The book could not be uploaded.');
    selectedBook = data;
    setBookStatus(data);
  } catch (error) {
    show(uploadZone);
    hide(bookStatus);
    if (window.location.protocol === 'file:') {
      setError('Please open the app at http://127.0.0.1:8000/ so it can connect to the book service.');
    } else {
      setError(error.message || 'The book could not be uploaded.');
    }
  }
}

async function uploadScene(file) {
  setError('');
  if (!selectedBook) return setError('Upload a book before visualizing a passage.');
  if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
    setError('Please choose a JPG, PNG, or WEBP photo.');
    return;
  }
  sceneStatus.innerHTML = '<span class="spinner small"></span><span>Reading the photographed passage…</span>';
  show(sceneStatus);
  const form = new FormData();
  form.append('file', file);
  hide(emptyState); hide(resultCard); show(processingState);
  processingTitle.textContent = 'Finding the photographed passage…';
  progressBar.style.width = '5%';
  try {
    const response = await fetch(`/api/books/${selectedBook.book_id}/scenes`, { method: 'POST', body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'The passage could not be read.');
    await pollJob(data.job_id, loadSceneResult);
  } catch (error) {
    hide(processingState); show(emptyState); setError(error.message); sceneStatus.textContent = '';
  }
}

async function askQuestion() {
  setError('');
  const question = questionInput.value.trim();
  if (!selectedBook) return setError('Upload a book before asking a question.');
  if (!question) return setError('Write a question about the book first.');
  hide(emptyState); hide(resultCard); hide(questionResult); show(processingState);
  processingTitle.textContent = 'Asking the book…';
  progressBar.style.width = '5%';
  try {
    const response = await fetch(`/api/books/${selectedBook.book_id}/questions`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'The question could not start.');
    await pollJob(data.job_id, loadQuestionResult);
  } catch (error) {
    hide(processingState); show(emptyState); setError(error.message);
  }
}

async function analyzeCharacter() {
  setError('');
  const character = characterSelect.value.trim();
  if (!selectedBook) return setError('Upload a book first.');
  if (!character) return;
  hide(emptyState); hide(resultCard); show(processingState);
  processingTitle.textContent = `Finding ${character}…`;
  progressBar.style.width = '5%';
  try {
    const response = await fetch(`/api/books/${selectedBook.book_id}/characters`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ character })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'The search could not start.');
    await pollJob(data.job_id);
  } catch (error) {
    hide(processingState); show(emptyState); setError(error.message);
  }
}

async function pollJob(jobId, onComplete = loadResult) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const job = await response.json();
  processingMessage.textContent = job.message || 'Working through the book.';
  progressBar.style.width = `${job.progress || 5}%`;
  if (job.status === 'completed' || job.status === 'no_evidence') {
    if (job.result_id) await onComplete(job.result_id);
    return;
  }
  if (job.status === 'failed') throw new Error(job.message || 'The search failed.');
  window.setTimeout(() => pollJob(jobId, onComplete).catch((error) => { hide(processingState); show(emptyState); setError(error.message); }), 1200);
}

async function loadSceneResult(resultId) {
  const response = await fetch(`/api/results/${resultId}`);
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'The scene could not be loaded.');
  currentResultId = null;
  hide(processingState); hide(emptyState); show(resultCard);
  resultBook.textContent = result.book_title;
  resultCharacter.textContent = 'Book scene';
  resultDescription.textContent = 'Matched passage: ' + result.matched_paragraph.text;
  sceneSource.innerHTML = `<p class="eyebrow">Nearby context</p><p>${(result.context || []).map((paragraph) => escapeHtml(paragraph.text)).join(' ')}</p>`;
  show(sceneSource);
  quotesButton.classList.add('hidden');
  characterImage.src = result.image_url;
  characterImage.alt = 'Generated scene from the book passage';
  show(characterImage);
  sceneStatus.innerHTML = '<span class="check">✓</span><span>Passage found and visualized.</span>';
  show(sceneStatus);
}

async function loadQuestionResult(resultId) {
  const response = await fetch(`/api/results/${resultId}`);
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'The answer could not be loaded.');
  hide(processingState); hide(emptyState); hide(resultCard); show(questionResult);
  questionResultBook.textContent = result.book_title;
  questionResultQuestion.textContent = `“${result.question}”`;
  questionAnswer.textContent = result.answer;
  questionCitations.innerHTML = result.citations?.length
    ? `<strong>Book references</strong><br>${result.citations.map((citation) => `Chapter ${citation.chapter_number}, paragraph ${citation.paragraph_number}${citation.reason ? ` — ${escapeHtml(citation.reason)}` : ''}`).join('<br>')}`
    : '<span>No exact supporting references were returned.</span>';
}

async function loadResult(resultId) {
  const response = await fetch(`/api/results/${resultId}`);
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || 'The result could not be loaded.');
  if (result.result_type === 'scene') return loadSceneResult(resultId);
  if (result.result_type === 'question') return loadQuestionResult(resultId);
  currentResultId = resultId;
  hide(processingState); hide(emptyState); show(resultCard);
  resultBook.textContent = selectedBook.title;
  resultCharacter.textContent = result.character;
  resultDescription.textContent = result.description.physical_description || 'No physical description was found.';
  if (result.description.book_context) {
    resultContext.textContent = `Book context: ${result.description.book_context}`;
    show(resultContext);
  } else {
    resultContext.textContent = '';
    hide(resultContext);
  }
  hide(sceneSource);
  quotesButton.classList.remove('hidden');
  if (result.image_url) {
    characterImage.src = result.image_url;
    characterImage.alt = `Portrait of ${result.character}`;
    show(characterImage);
  } else hide(characterImage);
  quotesButton.textContent = 'Show source quotes';
  quotesPanel.innerHTML = '';
  hide(quotesPanel);
}

quotesButton.addEventListener('click', async () => {
  if (!currentResultId) return;
  if (!quotesPanel.classList.contains('hidden')) { hide(quotesPanel); quotesButton.textContent = 'Show source quotes'; return; }
  quotesButton.textContent = 'Loading quotes…';
  const response = await fetch(`/api/results/${currentResultId}/quotes`);
  const data = await response.json();
  quotesPanel.innerHTML = data.quotes.length ? data.quotes.map((quote) => `<blockquote>“${escapeHtml(quote.text)}”<cite>Chapter ${quote.chapter_number} · Paragraph ${quote.paragraph_number}</cite></blockquote>`).join('') : '<p class="muted">No source quotes were found.</p>';
  show(quotesPanel); quotesButton.textContent = 'Hide source quotes';
});

bookInput.addEventListener('change', () => { if (bookInput.files[0]) uploadBook(bookInput.files[0]); });
['dragenter', 'dragover'].forEach((eventName) => uploadZone.addEventListener(eventName, (event) => { event.preventDefault(); uploadZone.classList.add('dragging'); }));
['dragleave', 'drop'].forEach((eventName) => uploadZone.addEventListener(eventName, (event) => { event.preventDefault(); uploadZone.classList.remove('dragging'); }));
uploadZone.addEventListener('drop', (event) => { if (event.dataTransfer.files[0]) uploadBook(event.dataTransfer.files[0]); });
characterSelect.addEventListener('change', analyzeCharacter);
characterMode.addEventListener('click', () => { modeChosen = true; setMode('character'); });
sceneMode.addEventListener('click', () => { modeChosen = true; setMode('scene'); });
questionMode.addEventListener('click', () => { modeChosen = true; setMode('question'); });
changeMode.addEventListener('click', () => { modeChosen = false; setMode(activeMode); });
questionSubmit.addEventListener('click', askQuestion);
questionInput.addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') askQuestion();
});
sceneInput.addEventListener('change', () => { if (sceneInput.files[0]) uploadScene(sceneInput.files[0]); });
['dragenter', 'dragover'].forEach((eventName) => sceneUploadZone.addEventListener(eventName, (event) => { event.preventDefault(); sceneUploadZone.classList.add('dragging'); }));
['dragleave', 'drop'].forEach((eventName) => sceneUploadZone.addEventListener(eventName, (event) => { event.preventDefault(); sceneUploadZone.classList.remove('dragging'); }));
sceneUploadZone.addEventListener('drop', (event) => { if (event.dataTransfer.files[0]) uploadScene(event.dataTransfer.files[0]); });
setMode('character');

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[character]));
}
