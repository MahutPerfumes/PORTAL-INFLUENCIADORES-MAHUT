import streamlit as st
import requests
from urllib.parse import urlencode, urlsplit, parse_qs

st.set_page_config(page_title='Portal do Influenciador | MAHUT', page_icon='✨', layout='wide')
st.markdown('''<style>
.stApp {background:#f8f6f2;color:#24201b}
[data-testid="stHeader"] {background:#f8f6f2}
h1,h2,h3 {color:#272019}
.ma-header {background:linear-gradient(110deg,#161411,#34291c);padding:32px;border-radius:16px;color:#f4e5c4;margin-bottom:24px}
.ma-logo {font-family:Georgia,serif;font-size:38px;letter-spacing:7px;color:#c7a36a}
.ma-small {color:#d5c5ac;letter-spacing:2px;font-size:11px}
.ma-title {font-size:30px;font-weight:600;color:#fff!important;margin-top:24px}
.ma-subtitle {color:#f4e5c4}
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
if 'ma_recovery_token' not in st.session_state:
    st.session_state.ma_recovery_token = None

def auth_request(method, endpoint, **kwargs):
    return requests.request(method, f'{supabase_url}/auth/v1/{endpoint}', headers=HEADERS, timeout=15, **kwargs)

# Recovery links must use token_hash in the URL query string.
# Supabase email template: {{ .SiteURL }}?token_hash={{ .TokenHash }}&type=recovery
params = st.query_params
token_hash = params.get('token_hash')
link_type = params.get('type')
if token_hash and link_type in ('recovery', 'invite', 'signup') and not st.session_state.ma_recovery_token:
    try:
        r = auth_request('POST', 'verify', json={'token_hash': token_hash, 'type': link_type})
        if r.ok and r.json().get('access_token'):
            st.session_state.ma_recovery_token = r.json()['access_token']
            st.query_params.clear()
        else:
            st.error('Link inválido ou expirado. Solicite outro link.')
            st.query_params.clear()
    except requests.RequestException:
        st.warning('Não foi possível verificar o link. Tente novamente.')

st.info('Ambiente de testes. Vendas e comissões reais permanecem bloqueadas até a validação do acesso individual.')

if st.session_state.ma_recovery_token:
    st.subheader('Definir nova senha')
    with st.form('nova_senha'):
        nova = st.text_input('Nova senha (mínimo de 8 caracteres)', type='password')
        confirmar = st.text_input('Confirme a nova senha', type='password')
        salvar = st.form_submit_button('Salvar senha', type='primary')
    if salvar:
        if len(nova) < 8 or nova != confirmar:
            st.warning('Use ao menos 8 caracteres e confirme a mesma senha.')
        else:
            try:
                r = requests.put(f'{supabase_url}/auth/v1/user', headers={**HEADERS, 'Authorization': f'Bearer {st.session_state.ma_recovery_token}'}, json={'password': nova}, timeout=15)
                if r.ok:
                    st.session_state.ma_recovery_token = None
                    st.success('Senha definida! Agora entre com seu e-mail e sua senha.')
                else:
                    st.error('Não foi possível salvar a senha. Solicite outro link.')
            except requests.RequestException:
                st.error('Falha de conexão. Tente novamente.')
elif st.session_state.ma_session is None:
    aba_entrar, aba_recuperar = st.tabs(['Entrar', 'Primeiro acesso / Esqueci minha senha'])
    with aba_entrar:
        with st.form('ma_login'):
            email = st.text_input('E-mail').strip()
            senha = st.text_input('Senha', type='password')
            entrar = st.form_submit_button('Entrar', type='primary')
        if entrar:
            if not email or not senha:
                st.warning('Informe seu e-mail e senha.')
            else:
                try:
                    r = auth_request('POST', 'token?grant_type=password', json={'email': email, 'password': senha})
                    if r.ok and r.json().get('access_token'):
                        dados = r.json()
                        st.session_state.ma_session = {'access_token': dados['access_token'], 'email': dados.get('user', {}).get('email', email)}
                        st.rerun()
                    else:
                        st.error('Não foi possível entrar. Confira o e-mail e a senha.')
                except requests.RequestException:
                    st.error('Serviço indisponível. Tente novamente.')
    with aba_recuperar:
        st.write('Informe o e-mail cadastrado para receber um link de criação ou recuperação de senha.')
        with st.form('recuperar'):
            email_recuperar = st.text_input('E-mail cadastrado').strip()
            enviar = st.form_submit_button('Enviar link')
        if enviar:
            if not email_recuperar or '@' not in email_recuperar:
                st.warning('Informe um e-mail válido.')
            else:
                try:
                    r = auth_request('POST', 'recover', params={'redirect_to': PORTAL_URL}, json={'email': email_recuperar})
                    if r.ok:
                        st.success('Se o e-mail estiver cadastrado, você receberá um link. Confira também o spam.')
                    else:
                        st.error('Não foi possível solicitar o link. Verifique a configuração de URLs do Supabase.')
                except requests.RequestException:
                    st.error('Falha de conexão. Tente novamente.')
else:
    sessao = st.session_state.ma_session
    try:
        validacao = requests.get(f'{supabase_url}/auth/v1/user', headers={**HEADERS, 'Authorization': f"Bearer {sessao['access_token']}"}, timeout=15)
    except requests.RequestException:
        st.warning('Não foi possível validar a sessão. Tente novamente.')
        st.stop()
    if not validacao.ok:
        st.session_state.ma_session = None
        st.warning('Sua sessão expirou. Entre novamente.')
        st.stop()
    usuario = validacao.json()
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
