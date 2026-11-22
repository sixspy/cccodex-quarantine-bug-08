import { api } from '../api.js';

function chipClass(b) {
  if (b.gate_active) return b.fingerprint_drifted ? 'bad' : 'ok';
  if (b.is_full) return 'bad';
  return '';
}

export async function renderBins(el) {
  const bins = await api('/api/bins');
  const rows = await api('/api/rows');
  el.innerHTML = `
    <div class="card">
      <h2>隔离舱 <span class="muted">满舱芯片与水位值同读审计账册</span></h2>
      <table>
        <thead><tr><th>舱号</th><th>标题</th><th>状态</th><th>水位/容量</th><th>待复核</th><th>满舱芯片</th><th>落点指纹</th><th>操作</th></tr></thead>
        <tbody>
          ${(bins.items || []).map((b) => `
            <tr>
              <td>${b.bin_code}</td>
              <td>${b.title}</td>
              <td><span class="chip">${b.status}</span></td>
              <td><b>${b.water_level}</b>/${b.capacity}</td>
              <td>${b.pending_count}</td>
              <td><span class="chip ${chipClass(b)}">${b.full_chip}</span></td>
              <td class="muted">${b.fingerprint ? b.fingerprint.slice(0, 12) + '…' : '—'}</td>
              <td>${b.is_full && !b.gate_active ? `<button data-gate="${b.bin_code}">落满舱闸</button>` : ''}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p id="gateMsg" class="err"></p>
    </div>
    <div class="card">
      <h2>舱内行</h2>
      <table>
        <thead><tr><th>舱</th><th>条目键</th><th>质量</th><th>原因</th><th>状态</th></tr></thead>
        <tbody>
          ${(rows.items || []).map((r) => `
            <tr>
              <td>${r.bin_code}</td>
              <td>${r.item_key}</td>
              <td>${r.quality}</td>
              <td>${r.reason}</td>
              <td><span class="chip">${r.status}</span></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  el.querySelectorAll('[data-gate]').forEach((btn) => {
    btn.onclick = async () => {
      const msg = el.querySelector('#gateMsg');
      msg.textContent = '';
      try {
        await api('/api/cabin_full/commit', {
          method: 'POST',
          body: JSON.stringify({ bin_code: btn.dataset.gate }),
        });
        await renderBins(el);
      } catch (e) {
        msg.textContent = e.message;
      }
    };
  });
}
