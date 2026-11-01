const base = document.body.dataset.api;
async function measure(button) {
  button.disabled = true;
  const status = button.nextElementSibling;
  status.textContent = 'Measuring…';
  try {
    const response = await fetch(base + button.dataset.id, {method: 'POST'});
    const result = await response.json();
    if (!response.ok) throw Error(result.detail || 'Measurement failed');
    status.textContent = 'Ready — refresh to view';
    button.dataset.ready = '1';
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
document.querySelectorAll('[data-id]').forEach(button => button.onclick = async () => {
  await measure(button);
  if (button.dataset.ready) location.reload();
});
document.querySelector('#all').onclick = async event => {
  event.target.disabled = true;
  for (const button of document.querySelectorAll('[data-id]')) {
    if (button.textContent === 'Measure hog ray') await measure(button);
  }
  event.target.textContent = 'Finished — refresh to view results';
  event.target.onclick = () => location.reload();
  event.target.disabled = false;
};
