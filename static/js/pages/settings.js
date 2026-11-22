import { api } from '../api.js';

export async function renderSettings(el) {
  const data = await api('/api/settings');
  el.innerHTML = `
    <div class="card">
      <h2>台参数</h2>
      <table>
        <thead><tr><th>项</th><th>说明</th><th>值</th><th></th></tr></thead>
        <tbody>
          ${(data.items || []).map((s) => `
            <tr>
              <td>${s.key}</td>
              <td>${s.label}</td>
              <td><input data-key="${s.key}" value="${s.value}" /></td>
              <td><button data-save="${s.key}">保存</button></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  el.querySelectorAll('[data-save]').forEach((btn) => {
    btn.onclick = async () => {
      const input = el.querySelector(`input[data-key="${btn.dataset.save}"]`);
      await api('/api/settings/update', {
        method: 'POST',
        body: JSON.stringify({ key: btn.dataset.save, value: input.value }),
      });
      await renderSettings(el);
    };
  });

  try {
    const cf = await api('/api/cabin_full/settings');
    el.innerHTML += `<div class="card" style="margin-top:1rem">
      <h3>${cf.title} <span class="muted">${cf.track_label}</span></h3>
      <div class="stat-strip">
        <div class="stat"><span class="muted">读轨</span><b>${cf.track}</b></div>
        <div class="stat"><span class="muted">满舱舱数</span><b>${cf.full_bin_count}</b></div>
        <div class="stat"><span class="muted">落点行</span><b>${cf.gates_total}</b></div>
        <div class="stat"><span class="muted">指纹偏移</span><b>${cf.drifted}</b></div>
      </div>
      <p class="muted">满舱口径只认审计账册：当刻舱码与水位指纹落定即只读，拒绝回刷；本页不提供双轨/归档切流操作。</p>
      <table>
        <thead><tr><th>舱码</th><th>水位/容量</th><th>指纹</th><th>版本</th><th>落定时间</th></tr></thead>
        <tbody>
          ${(cf.gates || []).map((g) => `
            <tr>
              <td>${g.bin_code}</td>
              <td>${g.water_level}/${g.capacity}</td>
              <td class="muted">${(g.fingerprint || '').slice(0, 16)}…</td>
              <td>${g.revision}</td>
              <td>${g.committed_at}</td>
            </tr>`).join('') || '<tr><td colspan="5" class="empty">暂无满舱落点</td></tr>'}
        </tbody>
      </table>
    </div>`;
  } catch (e) { /* optional surface */ }

}
