import streamlit as st
import requests
import psycopg2
from decimal import Decimal, InvalidOperation
from datetime import datetime, date

st.set_page_config(page_title='Portal do Influenciador | MAHUT', page_icon='✨', layout='wide', initial_sidebar_state='collapsed')

st.markdown('''<style>
:root {color-scheme:light!important}
html, body, [data-testid="stAppViewContainer"], .stApp {background:#f8f6f2!important;color:#24201b!important}
[data-testid="stHeader"], [data-testid="stToolbar"] {background:#f8f6f2!important}
[data-testid="stMainBlockContainer"] {max-width:1100px;padding-top:2rem;padding-bottom:3rem}
.stApp p,.stApp label,.stApp li,.stApp span,.stApp h1,.stApp h2,.stApp h3,.stApp small,
[data-testid="stMarkdownContainer"] {color:#24201b!important}
.ma-header {background:linear-gradient(110deg,#161411,#34291c)!important;padding:clamp(22px,4vw,36px);border-radius:16px;margin-bottom:24px}
.ma-header .ma-logo {font-family:Georgia,serif;font-size:clamp(29px,5vw,38px);letter-spacing:6px;color:#c7a36a!important}
.ma-header .ma-small {color:#d5c5ac!important;letter-spacing:2px;font-size:11px}
.ma-header .ma-title {font-size:clamp(22px,4vw,30px);font-weight:600;color:#fff!important;margin-top:18px}
.ma-header .ma-subtitle {color:#f4e5c4!important}
[data-testid="stTextInput"] input {background:#fff!important;color:#211c17!important;-webkit-text-fill-color:#211c17!important;border:1px solid #9a8b76!important;border-radius:7px!important;min-height:42px!important}
[data-testid="stTextInput"] input::placeholder {color:#6b6258!important;-webkit-text-fill-color:#6b6258!important;opacity:1!important}
[data-testid="stTextInput"] input:focus {border-color:#9a753e!important;box-shadow:0 0 0 2px #c7a36a55!important}
[data-testid="stTextInput"] button {color:#3b3125!important;background:#f0ece5!important}
[data-testid="stForm"] {background:#fff!important;border:1px solid #e2d9cb!important;border-radius:12px!important;padding:18px!important}
[data-testid="stForm"] label,[data-testid="stWidgetLabel"] p {color:#24201b!important;font-weight:600!important}
.stButton button[kind="primary"], [data-testid="stFormSubmitButton"] button[kind="primary"] {background:#30251a!important;color:#fff!important;border:1px solid #30251a!important}
.stButton button,[data-testid="stFormSubmitButton"] button {border-radius:8px!important;min-height:40px!important}
[data-baseweb="tab-list"] {gap:8px;flex-wrap:wrap}
button[data-baseweb="tab"] {background:#eee7dc!important;border-radius:8px!important}
button[data-baseweb="tab"] p {color:#30251a!important}
button[data-baseweb="tab"][aria-selected="true"] {background:#e2cba5!important}
[data-testid="stAlert"] p,[data-testid="stAlert"] span {color:#24201b!important}
[data-testid="stMetric"] {background:#fff!important;border:1px solid #e2d9cb;border-radius:10px;padding:15px}
[data-testid="stMetricLabel"] p,[data-testid="stMetricValue"] {color:#24201b!important}
@media(max-width:650px){[data-testid="stMainBlockContainer"]{padding-left:1rem;padding-right:1rem;padding-top:1rem}.ma-header{border-radius:12px}}
</style>''', unsafe_allow_html=True)

st.markdown('''<div class="ma-header"><div class="ma-logo">MAHUT</div>
<div class="ma-small">PERFUMES · PORTAL DO INFLUENCIADOR</div>
<div class="ma-title">Seu espaço exclusivo MAHUT</div>
<p class="ma-subtitle">Vendas, comissões e resultados em um só lugar.</p></div>''', unsafe_allow_html=True)

PORTAL_URL = 'https://app-influenciadores-mahut-6aqntxxznxkjnntsppmaft.streamlit.app/'
try:
    supabase_url = str(st.secrets['supabase']['url']).rstrip('/')
    publishable_key = str(st.secrets['supabase']['publishable_key'])
    if not supabase_url.startswith('https://') or not publishable_key:
        raise ValueError('Configuração inválida')
except (KeyError, ValueError, TypeError):
    st.error('A conexão de autenticação ainda não está configurada. Contate a MAHUT.')
    st.stop()

HEADERS = {'apikey': publishable_key, 'Content-Type': 'application/json'}
if 'ma_session' not in st.session_state:
    st.session_state.ma_session = None
if 'ma_recovery_step' not in st.session_state:
    st.session_state.ma_recovery_step = 'login'
if 'ma_recovery_email' not in st.session_state:
    st.session_state.ma_recovery_email = ''
if 'ma_recovery_token' not in st.session_state:
    st.session_state.ma_recovery_token = None

def auth_request(method, endpoint, **kwargs):
    return requests.request(method, f'{supabase_url}/auth/v1/{endpoint}', headers=HEADERS, timeout=20, **kwargs)



def buscar_vinculo_individual(usuario):
    """Somente servidor: valida UID do Auth contra auth.users e cadastro privado."""
    uri = str(st.secrets.get('supabase', {}).get('database_url', '')).strip()
    if not uri:
        raise RuntimeError('Falta database_url nos Secrets do Portal.')
    uid = str(usuario.get('id') or '')
    if not uid:
        raise RuntimeError('Usuário sem identidade validada.')
    with psycopg2.connect(uri, connect_timeout=10, sslmode='require') as con:
        with con.cursor() as cur:
            cur.execute("""SELECT p.id, p.nome, p.cupom, p.comissao_percentual, p.ativo
                           FROM auth.users AS u
                           JOIN mahut_private.influenciadores_bi AS p
                             ON lower(trim(p.email)) = lower(trim(u.email))
                           WHERE u.id = %s::uuid""", (uid,))
            linhas = cur.fetchall()
    if len(linhas) != 1:
        return None
    id_parceiro, nome, cupom, comissao, ativo = linhas[0]
    if not ativo:
        return None
    return {'id': str(id_parceiro), 'nome': nome, 'cupom': str(cupom).strip().upper(),
            'comissao': Decimal(str(comissao))}


def moeda_br(valor):
    return 'R$ ' + f'{valor:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def decimal_seguro(valor):
    try:
        d = Decimal(str(valor))
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def cupons_do_pedido(pedido):
    encontrados = set()
    def adicionar(v):
        if isinstance(v, str) and v.strip():
            encontrados.add(v.strip().upper())
        elif isinstance(v, dict):
            for campo in ('code', 'coupon_code', 'coupon', 'name'):
                x = v.get(campo)
                if isinstance(x, str) and x.strip():
                    encontrados.add(x.strip().upper())
    for campo in ('coupon', 'coupon_code', 'coupons', 'coupon_codes'):
        dado = pedido.get(campo)
        if isinstance(dado, list):
            for item in dado:
                adicionar(item)
        else:
            adicionar(dado)
    return encontrados


@st.cache_data(ttl=300, show_spinner=False)
def consultar_pedidos():
    cfg = st.secrets.get('nuvemshop', {})
    loja, token = str(cfg.get('store_id', '')).strip(), str(cfg.get('access_token', '')).strip()
    if not loja or not token:
        raise RuntimeError('Faltam store_id ou access_token da Nuvemshop nos Secrets do Portal.')
    headers = {'Authentication': f'bearer {token}', 'User-Agent': 'Portal MAHUT (contato@mahut.ind.br)'}
    resultado = []
    for pagina in range(1, 501):
        resposta = requests.get(f'https://api.nuvemshop.com.br/v1/{loja}/orders',
                                headers=headers, params={'page': pagina, 'per_page': 200}, timeout=30)
        resposta.raise_for_status()
        lote = resposta.json()
        if not isinstance(lote, list):
            raise RuntimeError('Resposta inesperada da Nuvemshop.')
        resultado.extend(lote)
        if len(lote) < 200:
            return resultado
    raise RuntimeError('Limite de paginação atingido; consulta incompleta.')


def resultados_do_parceiro(pedidos, parceiro, inicio, fim):
    """Isolamento por cupom validado no servidor; nunca envia pedidos brutos à interface."""
    linhas = []
    vistos = set()
    for pedido in pedidos:
        if not isinstance(pedido, dict):
            continue
        if str(pedido.get('payment_status') or '').lower().strip() != 'paid':
            continue
        if str(pedido.get('status') or '').lower().strip() in ('cancelled', 'canceled', 'cancelado'):
            continue
        try:
            dia = date.fromisoformat(str(pedido.get('created_at') or '')[:10])
        except ValueError:
            continue
        if not inicio <= dia <= fim:
            continue
        cupons = cupons_do_pedido(pedido)
        # Não atribuir vendas com múltiplos cupons: exige conciliação administrativa.
        if cupons != {parceiro['cupom']}:
            continue
        chave = str(pedido.get('id') or pedido.get('number') or '')
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        total = decimal_seguro(pedido.get('total'))
        frete = decimal_seguro(pedido.get('shipping_cost_customer'))
        if total is None or frete is None or total < frete:
            continue
        base = total - frete
        comissao = base * parceiro['comissao'] / Decimal('100')
        linhas.append({'Pedido': str(pedido.get('number') or chave),
                       'Data': dia.strftime('%d/%m/%Y'), 'base': base, 'comissao': comissao})
    return linhas

st.info('Ambiente de testes. Vendas e comissões reais permanecem bloqueadas até a validação do isolamento de dados.')

if st.session_state.ma_session is None and st.session_state.ma_recovery_step == 'request':
    st.subheader('Recuperar acesso ao Portal')
    st.write('Informe seu e-mail cadastrado para receber um código de recuperação.')
    with st.form('ma_pedir_recuperacao'):
        email_rec = st.text_input('E-mail cadastrado').strip().lower()
        enviar_rec = st.form_submit_button('Enviar código', type='primary')
    if enviar_rec:
        if not email_rec or '@' not in email_rec:
            st.warning('Informe um e-mail válido.')
        else:
            try:
                r = auth_request('POST', 'recover', json={'email': email_rec})
                if r.ok:
                    st.session_state.ma_recovery_email = email_rec
                    st.session_state.ma_recovery_step = 'verify'
                    st.rerun()
                elif r.status_code == 429:
                    st.warning('Muitas solicitações. Aguarde antes de tentar novamente.')
                else:
                    st.error('Não foi possível solicitar a recuperação agora. Tente novamente mais tarde.')
            except requests.RequestException:
                st.error('Falha de conexão. Tente novamente.')
    if st.button('Voltar ao login', key='ma_voltar_recovery1'):
        st.session_state.ma_recovery_step = 'login'
        st.rerun()
    st.stop()

if st.session_state.ma_session is None and st.session_state.ma_recovery_step == 'verify':
    st.subheader('Confirmar código de recuperação')
    st.caption('Confira o e-mail cadastrado. O código tem validade limitada.')
    with st.form('ma_verificar_codigo'):
        codigo = st.text_input('Código recebido por e-mail').strip()
        verificar = st.form_submit_button('Validar código', type='primary')
    if verificar:
        if not codigo:
            st.warning('Informe o código recebido.')
        else:
            try:
                r = auth_request('POST', 'verify', json={
                    'email': st.session_state.ma_recovery_email,
                    'token': codigo, 'type': 'recovery'})
                dados = r.json() if r.ok else {}
                if r.ok and dados.get('access_token'):
                    st.session_state.ma_recovery_token = dados['access_token']
                    st.session_state.ma_recovery_step = 'new_password'
                    st.rerun()
                else:
                    st.error('Código inválido ou expirado. Solicite outro código.')
            except (requests.RequestException, ValueError):
                st.error('Não foi possível validar o código. Tente novamente.')
    if st.button('Solicitar outro código', key='ma_reenviar'):
        st.session_state.ma_recovery_step = 'request'
        st.rerun()
    st.stop()

if st.session_state.ma_session is None and st.session_state.ma_recovery_step == 'new_password':
    st.subheader('Criar nova senha')
    with st.form('ma_nova_senha_recuperacao'):
        nova_rec = st.text_input('Nova senha (mínimo de 12 caracteres)', type='password')
        confirmar_rec = st.text_input('Confirme a nova senha', type='password')
        salvar_rec = st.form_submit_button('Salvar nova senha', type='primary')
    if salvar_rec:
        if len(nova_rec) < 12 or nova_rec != confirmar_rec:
            st.warning('Use ao menos 12 caracteres e confirme a mesma senha.')
        elif not st.session_state.ma_recovery_token:
            st.error('A validação expirou. Solicite um novo código.')
        else:
            try:
                token = st.session_state.ma_recovery_token
                r = requests.put(f'{supabase_url}/auth/v1/user',
                    headers={**HEADERS, 'Authorization': f'Bearer {token}'},
                    json={'password': nova_rec, 'data': {'ma_primeiro_acesso': False}}, timeout=20)
                if r.ok:
                    st.session_state.ma_recovery_token = None
                    st.session_state.ma_recovery_email = ''
                    st.session_state.ma_recovery_step = 'login'
                    st.success('Senha alterada. Entre com a nova senha.')
                else:
                    st.error('Não foi possível alterar a senha. Solicite um novo código.')
            except requests.RequestException:
                st.error('Falha de conexão. Tente novamente.')
    st.stop()

if st.session_state.ma_session is None:
    st.subheader('Entrar no Portal')
    with st.form('ma_login'):
        email = st.text_input('E-mail').strip().lower()
        senha = st.text_input('Senha', type='password')
        entrar = st.form_submit_button('Entrar', type='primary')
    if entrar:
        if not email or not senha:
            st.warning('Informe e-mail e senha.')
        else:
            try:
                r = auth_request('POST', 'token?grant_type=password', json={'email': email, 'password': senha})
                if r.ok and r.json().get('access_token'):
                    dados = r.json()
                    st.session_state.ma_session = {'access_token': dados['access_token'], 'email': email}
                    st.rerun()
                else:
                    st.error('E-mail ou senha incorretos. Contate a MAHUT para obter acesso.')
            except requests.RequestException:
                st.error('Falha de conexão. Tente novamente.')
    if st.button('Esqueci minha senha', key='ma_esqueci_senha'):
        st.session_state.ma_recovery_step = 'request'
        st.rerun()
    st.caption('No primeiro acesso, utilize a senha provisória fornecida pela MAHUT.')
else:
    sessao = st.session_state.ma_session
    try:
        resposta = requests.get(f'{supabase_url}/auth/v1/user', headers={**HEADERS, 'Authorization': f"Bearer {sessao['access_token']}"}, timeout=20)
    except requests.RequestException:
        st.error('Falha ao validar sua sessão.')
        st.stop()
    if not resposta.ok:
        st.session_state.ma_session = None
        st.warning('Sessão expirada. Entre novamente.')
        st.stop()
    usuario = resposta.json()
    metadata = usuario.get('user_metadata') or {}
    if metadata.get('ma_primeiro_acesso', False):
        st.subheader('Definir sua senha definitiva')
        st.write('Por segurança, é obrigatório trocar a senha provisória antes de continuar.')
        with st.form('ma_primeira_senha'):
            nova = st.text_input('Nova senha (mínimo de 12 caracteres)', type='password')
            confirmar = st.text_input('Confirme a nova senha', type='password')
            salvar = st.form_submit_button('Salvar nova senha', type='primary')
        if salvar:
            if len(nova) < 12 or nova != confirmar:
                st.warning('Use ao menos 12 caracteres e confirme a mesma senha.')
            else:
                try:
                    # Atualiza senha e marcador em uma única solicitação autenticada.
                    r = requests.put(f'{supabase_url}/auth/v1/user',
                        headers={**HEADERS, 'Authorization': f"Bearer {sessao['access_token']}"},
                        json={'password': nova, 'data': {**metadata, 'ma_primeiro_acesso': False}}, timeout=20)
                    if r.ok:
                        st.session_state.ma_session = None
                        st.success('Senha atualizada. Entre novamente com sua senha definitiva.')
                        st.rerun()
                    else:
                        st.error('Não foi possível atualizar a senha. Tente novamente.')
                except requests.RequestException:
                    st.error('Falha de conexão. Tente novamente.')
        if st.button('Sair', key='ma_sair_primeiro'):
            st.session_state.ma_session = None
            st.rerun()
        st.stop()
    st.success('Acesso autenticado ao Portal MAHUT.')
    if st.button('Sair'):
        st.session_state.ma_session = None
        st.rerun()
    try:
        parceiro = buscar_vinculo_individual(usuario)
    except Exception:
        st.error('Não foi possível validar seu vínculo com a MAHUT. Entre em contato com a administração.')
        st.stop()
    if parceiro is None:
        st.warning('Conta sem vínculo ativo com um influenciador cadastrado. Contate a MAHUT.')
        st.stop()
    st.subheader(f"Olá, {parceiro['nome']}")
    st.caption(f"Cupom: {parceiro['cupom']} · Comissão: {str(parceiro['comissao']).replace('.', ',')}%")
    st.info('Vínculo individual identificado. Os valores reais continuam bloqueados até a homologação com duas contas diferentes.')
    if not bool(st.secrets.get('portal', {}).get('liberar_resultados', False)):
        st.caption('Valide que cada conta recebe apenas o seu nome e cupom. Depois, a MAHUT poderá liberar os indicadores.')
        st.stop()
    hoje = date.today()
    col1, col2 = st.columns(2)
    with col1:
        inicio = st.date_input('De', value=hoje.replace(day=1), format='DD/MM/YYYY')
    with col2:
        fim = st.date_input('Até', value=hoje, format='DD/MM/YYYY')
    if inicio > fim:
        st.warning('A data inicial deve ser anterior à final.')
        st.stop()
    try:
        linhas = resultados_do_parceiro(consultar_pedidos(), parceiro, inicio, fim)
    except Exception:
        st.error('Não foi possível consultar os pedidos da Nuvemshop. Confira a configuração do Portal.')
        st.stop()
    faturamento = sum((x['base'] for x in linhas), Decimal('0'))
    comissao = sum((x['comissao'] for x in linhas), Decimal('0'))
    st.divider()
    a, b, c = st.columns(3)
    with a: st.metric('Meus pedidos pagos', len(linhas))
    with b: st.metric('Faturamento atribuído', moeda_br(faturamento))
    with c: st.metric('Comissão estimada', moeda_br(comissao))
    st.caption('Comissão estimada sobre mercadorias após descontos, sem frete. Saldo a receber depende de conciliação financeira e não é exibido.')
    if linhas:
        st.subheader('Meus pedidos')
        st.dataframe([{'Pedido': x['Pedido'], 'Data': x['Data'],
                       'Faturamento atribuído': moeda_br(x['base']),
                       'Comissão estimada': moeda_br(x['comissao'])} for x in linhas],
                     hide_index=True, use_container_width=True)
    else:
        st.info('Nenhum pedido pago atribuído ao seu cupom no período selecionado.')
    st.caption('Nenhum nome, endereço, telefone ou e-mail de cliente é exibido.')
