import streamlit as st
import requests

st.set_page_config(page_title="Portal do Influenciador | MAHUT", page_icon="✨", layout="wide")

st.markdown("""<style>
.stApp {background:#f8f6f2;color:#24201b}
[data-testid="stHeader"] {background:#f8f6f2}
h1,h2,h3 {color:#272019}
.ma-header {background:linear-gradient(110deg,#161411,#34291c);padding:32px;border-radius:16px;color:#f4e5c4;margin-bottom:24px}
.ma-logo {font-family:Georgia,serif;font-size:38px;letter-spacing:7px;color:#c7a36a}
.ma-small {color:#d5c5ac;letter-spacing:2px;font-size:11px}
.ma-title {font-size:30px;font-weight:600;color:#fff!important;margin-top:24px}
.ma-subtitle {color:#f4e5c4}
</style>""", unsafe_allow_html=True)

st.markdown("""<div class="ma-header"><div class="ma-logo">MAHUT</div>
<div class="ma-small">PERFUMES · PORTAL DO INFLUENCIADOR</div>
<div class="ma-title">Seu espaço exclusivo MAHUT</div>
<p class="ma-subtitle">Vendas, comissões e resultados em um só lugar.</p></div>""", unsafe_allow_html=True)

try:
    supabase_url = str(st.secrets["supabase"]["url"]).rstrip("/")
    publishable_key = str(st.secrets["supabase"]["publishable_key"])
    if not supabase_url.startswith("https://") or not publishable_key:
        raise ValueError("Configuração inválida")
except (KeyError, ValueError, TypeError):
    st.error("A conexão de autenticação ainda não está configurada. Contate a MAHUT.")
    st.stop()

if "ma_session" not in st.session_state:
    st.session_state.ma_session = None

st.info("Ambiente de testes. Vendas e comissões reais ainda estão bloqueadas até a validação das regras de acesso individual.")

if st.session_state.ma_session is None:
    st.subheader("Acesso do influenciador")
    with st.form("ma_login"):
        email = st.text_input("E-mail", placeholder="seuemail@exemplo.com").strip()
        senha = st.text_input("Senha", type="password")
        entrar = st.form_submit_button("Entrar", type="primary")
    if entrar:
        if not email or not senha:
            st.warning("Informe seu e-mail e senha.")
        else:
            try:
                resposta = requests.post(
                    f"{supabase_url}/auth/v1/token?grant_type=password",
                    headers={"apikey": publishable_key, "Content-Type": "application/json"},
                    json={"email": email, "password": senha},
                    timeout=15,
                )
                if resposta.status_code == 200:
                    dados = resposta.json()
                    token = dados.get("access_token")
                    if not token:
                        st.error("Não foi possível validar o acesso.")
                    else:
                        st.session_state.ma_session = {"access_token": token, "email": dados.get("user", {}).get("email", email)}
                        st.rerun()
                else:
                    st.error("Não foi possível entrar. Confira suas credenciais e a confirmação da conta.")
            except requests.RequestException:
                st.error("Serviço de autenticação indisponível. Tente novamente.")
    st.caption("Recebeu um convite? A ativação de senha por convite será configurada na próxima etapa.")
else:
    sessao = st.session_state.ma_session
    try:
        validacao = requests.get(
            f"{supabase_url}/auth/v1/user",
            headers={"apikey": publishable_key, "Authorization": f"Bearer {sessao['access_token']}"},
            timeout=15,
        )
    except requests.RequestException:
        st.warning("Não foi possível validar a sessão. Tente novamente.")
        st.stop()
    if validacao.status_code != 200:
        st.session_state.ma_session = None
        st.warning("Sua sessão expirou. Entre novamente.")
        st.stop()
    usuario = validacao.json()
    st.success(f"Acesso autenticado: {usuario.get('email', 'Influenciador MAHUT')}")
    st.warning("O vínculo da conta ao cupom e a consulta individual de vendas ainda não foram habilitados.")
    if st.button("Sair"):
        st.session_state.ma_session = None
        st.rerun()

st.divider()
a, b, c = st.columns(3)
with a:
    st.metric("Minhas vendas", "—")
with b:
    st.metric("Minhas comissões", "—")
with c:
    st.metric("Saldo a receber", "—")
st.caption("Indicadores reais serão habilitados somente após testes de isolamento de dados por influenciador.")
