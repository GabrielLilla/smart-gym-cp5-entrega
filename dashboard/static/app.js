// Smart Gym - CP05 | Front-end do dashboard (recebe eventos via SSE)
const $ = (id) => document.getElementById(id);
let estado = { broker: {}, estacoes: {}, series: [], acessos: [] };

const hora = (iso) => (iso ? new Date(iso).toLocaleTimeString('pt-BR') : '-');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

const MOTIVOS = {
  entrada: 'Entrada liberada',
  saida: 'Saída',
  nao_cadastrado: 'Cartão não cadastrado',
  estacao_ocupada: 'Estação ocupada',
};

function renderBroker() {
  const on = estado.broker.conectado;
  $('broker').className = `pill ${on ? 'pill-on' : 'pill-off'}`;
  $('broker').textContent = `Broker ${on ? 'conectado' : 'desconectado'}`;
}

function renderEstacoes() {
  const lista = Object.values(estado.estacoes).sort((a, b) => a.id.localeCompare(b.id));
  if (!lista.length) return;
  $('estacoes').innerHTML = lista.map((e) => {
    const offline = e.conexao === 'offline';
    const classe = offline ? 'offline' : e.status;
    const rotulo = offline ? 'OFFLINE' : e.status === 'ocupada' ? 'OCUPADA' : e.status === 'livre' ? 'LIVRE' : '—';
    return `
      <article class="estacao ${esc(classe)}">
        <div class="topo">
          <span class="nome">${esc(e.id)}</span>
          <span class="conexao">${esc(e.conexao)} · ${hora(e.atualizadoEm)}</span>
        </div>
        <div class="indicador"><div class="luz"></div><div class="rotulo">${rotulo}</div></div>
        <div class="info">
          <div><span>Aluno</span><b>${esc(e.aluno) || '—'}</b></div>
          <div><span>Séries</span><b>${e.serie || 0}</b></div>
          <div class="reps"><span>Reps (última série)</span><b>${e.ultimas_reps ?? '—'}</b></div>
        </div>
        <button data-id="${esc(e.id)}" ${e.status === 'ocupada' && !offline ? '' : 'disabled'}>Liberar remotamente (MQTT cmd)</button>
      </article>`;
  }).join('');
}

function renderSeries(destacar) {
  $('series').innerHTML = estado.series.map((s, i) => `
    <tr class="${destacar && i === 0 ? 'novo' : ''}">
      <td>${hora(s.recebidoEm)}</td><td>${esc(s.estacao)}</td><td>${esc(s.aluno)}</td>
      <td>${s.serie}</td><td><b>${s.repeticoes}</b></td><td class="${s.invalidadas ? 'negado' : ''}">${s.invalidadas ?? 0}</td><td>${s.duracao_s ?? '-'} s</td>
    </tr>`).join('') || '<tr><td colspan="7" class="vazio">Nenhuma série ainda</td></tr>';
}

function renderAcessos(destacar) {
  $('acessos').innerHTML = estado.acessos.map((a, i) => `
    <tr class="${destacar && i === 0 ? 'novo' : ''}">
      <td>${hora(a.recebidoEm)}</td><td>${esc(a.estacao)}</td><td><code>${esc(a.uid)}</code></td><td>${esc(a.aluno)}</td>
      <td class="${a.autorizado ? 'ok' : 'negado'}">${esc(MOTIVOS[a.motivo] || a.motivo)}</td>
    </tr>`).join('') || '<tr><td colspan="5" class="vazio">Nenhum acesso ainda</td></tr>';
}

function renderTudo() {
  renderBroker();
  renderEstacoes();
  renderSeries(false);
  renderAcessos(false);
}

const fonte = new EventSource('/events');

fonte.addEventListener('snapshot', (ev) => {
  estado = JSON.parse(ev.data);
  renderTudo();
});

fonte.addEventListener('broker', (ev) => {
  estado.broker = JSON.parse(ev.data);
  renderBroker();
});

fonte.addEventListener('mqtt', (ev) => {
  const { tipo, estacao, dados, recebidoEm, estado: est } = JSON.parse(ev.data);
  estado.estacoes[estacao] = est;
  if (tipo === 'serie') {
    estado.series.unshift({ ...dados, estacao, recebidoEm });
    estado.series = estado.series.slice(0, 50);
    renderSeries(true);
  }
  if (tipo === 'acesso') {
    estado.acessos.unshift({ ...dados, estacao, recebidoEm });
    estado.acessos = estado.acessos.slice(0, 50);
    renderAcessos(true);
  }
  renderEstacoes();
});

fonte.onerror = () => {
  estado.broker.conectado = false;
  renderBroker();
};

$('estacoes').addEventListener('click', async (ev) => {
  const id = ev.target.dataset?.id;
  if (!id) return;
  ev.target.disabled = true;
  await fetch(`/api/estacoes/${encodeURIComponent(id)}/liberar`, { method: 'POST' });
});
