import streamlit as st
import requests

st.set_page_config(page_title='Portal do Influenciador | MAHUT', page_icon='✨', layout='wide')
st.markdown('''<style>
:root { color-scheme: light; }
html, body, [data-testid="stAppViewContainer"], .stApp {
  background-color: #f8f6f2 !important;
  color: #24201b !important;
}
[data-testid="stHeader"] { background-color: #f8f6f2 !important; }
[data-testid="stMainBlockContainer"] { max-width: 1080px; padding-top: 2rem; }
[data-testid="stMarkdownContainer"], [data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li, .stApp h1, .stApp h2, .stApp h3,
[data-testid="stWidgetLabel"] p, [data-testid="stCaptionContainer"] p,
[data-testid="stMetricLabel"] p, [data-testid="stMetricValue"] {
  color: #24201b !important;
}
/* Explicit form colors so light/dark device settings cannot hide input text. */
.stApp [data-testid="stTextInput"] input,
.stApp [data-testid="stTextInput"] input:focus {
  background-color: #fff !important;
  color: #1f1b17 !important;
  -webkit-text-fill-color: #1f1b17 !important;
  caret-color: #1f1b17 !important;
}
.stApp [data-testid="stTextInput"] input::placeholder {
  color: #625b53 !important;
  -webkit-text-fill-color: #625b53 !important;
  opacity: 1 !important;
}
.stApp [data-testid="stTextInput"] [data-baseweb="input"],
.stApp [data-testid="stTextInput"] [data-baseweb="base-input"] {
  background: #fff !important;
  border-radius: 8px !important;
}
.stApp [data-testid="stTextInput"] [data-baseweb="input"] {
  border: 1px solid #8d8173 !important;
}
.stApp [data-testid="stTextInput"] button,
.stApp [data-testid="stTextInput"] svg { color: #34302a !important; fill: none; }
.stApp [data-testid="stForm"] {
  background: #fffdf9 !important;
  border: 1px solid #ded5c9 !important;
  border-radius: 12px !important;
  padding: 1.2rem !important;
}
.stApp [data-testid="stTabs"] [role="tab"] { color: #3d352b !important; }
.stApp [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  color: #6b4c20 !important;
  font-weight: 700 !important;
}
.stApp [data-testid="stButton"] button[kind="primary"],
.stApp [data-testid="stFormSubmitButton"] button[kind="primary"] {
  background: #49351c !important; color: #fff !important;
  border: 1px solid #49351c !important;
}
.stApp [data-testid="stButton"] button[kind="primary"] p,
.stApp [data-testid="stFormSubmitButton"] button[kind="primary"] p {
  color: #fff !important;
}
.stApp [data-testid="stButton"] button:not([kind="primary"]),
.stApp [data-testid="stFormSubmitButton"] button:not([kind="primary"]) {
  background: #fff !important; color: #24201b !important;
  border: 1px solid #8d8173 !important;
}
.ma-header {
  background: linear-gradient(110deg,#161411,#34291c);
  padding: clamp(22px, 4vw, 34px); border-radius: 16px;
  margin-bottom: 24px;
}
.ma-logo {font-family: Georgia,serif;font-size:clamp(28px,5vw,38px);letter-spacing:6px;color:#e4c18a!important}
.ma-small {color:#e3d0b4!important;letter-spacing:2px;font-size:11px}
.ma-title {font-size:clamp(21px,4vw,30px);font-weight:600;color:#fff!important;margin-top:20px}
.ma-subtitle, .ma-header .ma-subtitle {color:#f4e5c4!important; opacity:1!important}
@media(max-width:640px) {
  [data-testid="stMainBlockContainer"] {padding-left: 1rem; padding-right: 1rem;}
  .ma-header {border-radius: 12px;}
  .ma-logo {letter-spacing:4px;}
}
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

# O link padrão do Supabase pode trazer credenciais no fragmento (#),
# que o Streamlit não consegue ler. Como alternativa, o influenciador
# copia o endereço ORIGINAL do botão do e-mail e cola no formulário abaixo.
from urllib.parse import urlparse, parse_qs


def verificar_link_copiado(link):
    """Aceita exclusivamente um link de verificação do próprio projeto Supabase."""
    parsed = urlparse(link.strip())
    base = urlparse(supabase_url)
    if parsed.scheme != 'https' or parsed.netloc != base.netloc:
        raise ValueError('O link não pertence ao Supabase configurado para a MAHUT.')
    if parsed.path.rstrip('/') != '/auth/v1/verify':
        raise ValueError('Copie o endereço original do botão do e-mail, antes de abri-lo.')
    q = parse_qs(parsed.query)
    tipo = q.get('type', [''])[0]
    token = q.get('token', [''])[0] or q.get('token_hash', [''])[0]
    if tipo not in ('recovery', 'invite', 'signup') or not token:
        raise ValueError('O link não contém um código de recuperação ou convite válido.')
    return token, tipo


params = st.query_params
hash_recebido = params.get('token_hash')
tipo_recebido = params.get('type')
if hash_recebido and tipo_recebido in ('recovery', 'invite', 'signup') and not st.session_state.ma_recovery_token:
    try:
        resposta = auth_request('POST', 'verify', json={'token_hash': hash_recebido, 'type': tipo_recebido})
        if resposta.ok and resposta.json().get('access_token'):
            st.session_state.ma_recovery_token = resposta.json()['access_token']
            st.query_params.clear()
            st.rerun()
        else:
            st.error('Link inválido ou expirado. Solicite um novo e-mail.')
            st.query_params.clear()
    except requests.RequestException:
        st.error('Não foi possível validar o link. Tente novamente.')

if params.get('error') or params.get('error_description'):
    st.error('O link foi recusado ou expirou. Solicite outro e-mail.')
    st.query_params.clear()

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
                resposta = requests.put(
                    f'{supabase_url}/auth/v1/user',
                    headers={**HEADERS, 'Authorization': f'Bearer {st.session_state.ma_recovery_token}'},
                    json={'password': nova}, timeout=15,
                )
                if resposta.ok:
                    st.session_state.ma_recovery_token = None
                    st.success('Senha definida com sucesso. Entre com seu e-mail e a nova senha.')
                    st.rerun()
                else:
                    st.error('Não foi possível salvar a senha. Solicite um novo link.')
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
                    resposta = auth_request('POST', 'token?grant_type=password', json={'email': email, 'password': senha})
                    if resposta.ok and resposta.json().get('access_token'):
                        dados = resposta.json()
                        st.session_state.ma_session = {
                            'access_token': dados['access_token'],
                            'email': dados.get('user', {}).get('email', email),
                        }
                        st.rerun()
                    else:
                        st.error('Não foi possível entrar. Confira seu e-mail e senha.')
                except requests.RequestException:
                    st.error('Serviço indisponível. Tente novamente.')

    with aba_recuperar:
        st.write('Solicite um novo e-mail ou utilize um convite de primeiro acesso recebido da MAHUT.')
        with st.form('recuperar'):
            email_recuperar = st.text_input('E-mail cadastrado').strip()
            enviar = st.form_submit_button('Enviar link de recuperação')
        if enviar:
            if not email_recuperar or '@' not in email_recuperar:
                st.warning('Informe um e-mail válido.')
            else:
                try:
                    resposta = auth_request('POST', 'recover', params={'redirect_to': PORTAL_URL}, json={'email': email_recuperar})
                    if resposta.ok:
                        st.success('Se a conta existir, você receberá um e-mail. Confira também o spam.')
                    elif resposta.status_code == 429:
                        st.error('Limite temporário de envios atingido. Aguarde e tente novamente.')
                    else:
                        st.error('Não foi possível solicitar a recuperação. Consulte os registros de autenticação do Supabase.')
                except requests.RequestException:
                    st.error('Falha de conexão. Tente novamente.')

        st.markdown('#### Já recebeu o e-mail?')
        st.write('**Não clique no botão do e-mail.** Clique nele com o botão direito e escolha **Copiar endereço do link**. Depois cole o endereço abaixo. Não compartilhe esse link com ninguém.')
        with st.form('link_copiado'):
            link_original = st.text_input('Cole o endereço do botão Reset password ou Accept the invite', type='password')
            usar_link = st.form_submit_button('Validar link e definir senha', type='primary')
        if usar_link:
            try:
                token, tipo = verificar_link_copiado(link_original)
                resposta = auth_request('POST', 'verify', json={'token_hash': token, 'type': tipo})
                if resposta.ok and resposta.json().get('access_token'):
                    st.session_state.ma_recovery_token = resposta.json()['access_token']
                    st.rerun()
                else:
                    st.error('Link inválido, já utilizado ou expirado. Solicite outro e-mail.')
            except ValueError as exc:
                st.warning(str(exc))
            except requests.RequestException:
                st.error('Não foi possível validar o link. Tente novamente.')

else:
    sessao = st.session_state.ma_session
    try:
        validacao = requests.get(
            f'{supabase_url}/auth/v1/user',
            headers={**HEADERS, 'Authorization': f"Bearer {sessao['access_token']}"}, timeout=15,
        )
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
with a:
    st.metric('Minhas vendas', '—')
with b:
    st.metric('Minhas comissões', '—')
with c:
    st.metric('Saldo a receber', '—')
st.caption('Indicadores reais serão habilitados somente após testes de isolamento de dados por influenciador.')
