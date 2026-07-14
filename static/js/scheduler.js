// Auto-dismiss flash alerts after 5 s
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.alert-toast').forEach(el => {
    setTimeout(() => {
      const bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      bsAlert.close();
    }, 5000);
  });
});

// ── Shift picker ─────────────────────────────────────────────────────────
// Custom combobox for choosing a shift template: a native <select> can't
// color just the dot in an <option> while leaving the name/time plain (and
// can't color anything at all in its own closed-box display), so this
// hand-builds the dropdown instead.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.shift-picker').forEach(picker => {
    const toggle = picker.querySelector('.shift-picker-toggle');
    const label = picker.querySelector('.shift-picker-label');
    const menu = picker.querySelector('.shift-picker-menu');
    const hidden = picker.querySelector('input[type=hidden]');

    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      if (toggle.disabled) return;
      const willOpen = menu.classList.contains('d-none');
      document.querySelectorAll('.shift-picker-menu').forEach(m => m.classList.add('d-none'));
      menu.classList.toggle('d-none', !willOpen);
    });

    menu.querySelectorAll('.shift-picker-option').forEach(opt => {
      opt.addEventListener('click', () => {
        hidden.value = opt.dataset.value;
        label.innerHTML = opt.innerHTML;
        menu.classList.add('d-none');
        hidden.dispatchEvent(new Event('change', { bubbles: true }));
      });
    });
  });

  document.addEventListener('click', () => {
    document.querySelectorAll('.shift-picker-menu').forEach(m => m.classList.add('d-none'));
  });
});

// ── Excluded-dates calendar ──────────────────────────────────────────────
// A click-to-toggle month calendar paired with the existing comma-separated
// date text input — either can be used, and they stay in sync.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.excluded-dates-calendar').forEach(initExcludedDatesCalendar);
});

function initExcludedDatesCalendar(container) {
  const input = document.getElementById(container.dataset.input);
  const cursor = new Date();
  cursor.setDate(1);
  let selected = parseDates(input.value);

  function parseDates(value) {
    return new Set((value || '').split(',').map(s => s.trim()).filter(Boolean));
  }

  function fmt(y, m, d) {
    return `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
  }

  function syncInput() {
    input.value = Array.from(selected).sort().join(', ');
  }

  function render() {
    const y = cursor.getFullYear(), m = cursor.getMonth();
    const startOffset = (new Date(y, m, 1).getDay() + 6) % 7; // Monday-first
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    const today = new Date();
    const todayStr = fmt(today.getFullYear(), today.getMonth(), today.getDate());
    const monthLabel = cursor.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

    let html = `
      <div class="exd-cal-header">
        <button type="button" class="btn btn-sm btn-outline-secondary exd-cal-prev">&lsaquo;</button>
        <span class="exd-cal-label">${monthLabel}</span>
        <button type="button" class="btn btn-sm btn-outline-secondary exd-cal-next">&rsaquo;</button>
      </div>
      <div class="exd-cal-grid">`;
    ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'].forEach(d => { html += `<div class="exd-cal-dow">${d}</div>`; });
    for (let i = 0; i < startOffset; i++) html += '<div class="exd-cal-day exd-cal-empty"></div>';
    for (let d = 1; d <= daysInMonth; d++) {
      const ds = fmt(y, m, d);
      const cls = ['exd-cal-day'];
      if (selected.has(ds)) cls.push('excluded');
      if (ds === todayStr) cls.push('today');
      html += `<div class="${cls.join(' ')}" data-date="${ds}">${d}</div>`;
    }
    html += '</div>';
    container.innerHTML = html;

    container.querySelector('.exd-cal-prev').addEventListener('click', () => {
      cursor.setMonth(cursor.getMonth() - 1);
      render();
    });
    container.querySelector('.exd-cal-next').addEventListener('click', () => {
      cursor.setMonth(cursor.getMonth() + 1);
      render();
    });
    container.querySelectorAll('.exd-cal-day[data-date]').forEach(cell => {
      cell.addEventListener('click', () => {
        const ds = cell.dataset.date;
        if (selected.has(ds)) selected.delete(ds); else selected.add(ds);
        syncInput();
        render();
      });
    });
  }

  input.addEventListener('change', () => {
    selected = parseDates(input.value);
    render();
  });

  render();
}
