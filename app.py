import streamlit as st
import requests

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

def auth_request(method, endpoint, **kwargs):
    return requests.request(method, f'{supabase_url}/auth/v1/{endpoint}', headers=HEADERS, timeout=20, **kwargs)

st.info('Ambiente de testes. Vendas e comissões reais permanecem bloqueadas até a validação do isolamento de dados.')

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
    st.success(f"Acesso autenticado: {usuario.get('email', 'Influenciador MAHUT')}")
    st.warning('O vínculo ao cupom e a consulta individual de vendas ainda não foram habilitados.')
    if st.button('Sair'):
        st.session_state.ma_session = None
        st.rerun()

st.divider()
a, b, c = st.columns(3)
with a: st.metric('Minhas vendas', '—')
with b: st.metric('Minhas comissões', '—')
with c: st.metric('Saldo a receber', '—')
st.caption('Indicadores reais serão habilitados somente após testes de isolamento de dados por influenciador.')
