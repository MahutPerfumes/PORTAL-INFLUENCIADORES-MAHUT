import streamlit as st

st.set_page_config(page_title='Portal do Influenciador | MAHUT', page_icon='✨', layout='wide')

st.markdown('''<style>
.stApp {background: #f8f6f2; color: #24201b;}
[data-testid="stHeader"] {background: #f8f6f2;}
h1,h2,h3 {color:#272019 !important;}
.ma-header {background:linear-gradient(110deg,#161411,#34291c);padding:32px;border-radius:16px;color:#f4e5c4;margin-bottom:24px;}
.ma-logo {font-family:Georgia,serif;font-size:38px;letter-spacing:7px;color:#c7a36a;}
.ma-small {color:#d5c5ac;letter-spacing:2px;font-size:11px;}
.ma-note {border:1px solid #dfd5c6;background:white;padding:20px;border-radius:12px;}
</style>''', unsafe_allow_html=True)

st.markdown('''<div class="ma-header"><div class="ma-logo">MAHUT</div><div class="ma-small">PERFUMES · PORTAL DO INFLUENCIADOR</div><h2 style="color:white!important;margin-top:24px">Seu espaço exclusivo MAHUT</h2><p>Vendas, comissões e resultados em um só lugar.</p></div>''', unsafe_allow_html=True)

st.info('Portal em preparação. O acesso a dados reais permanecerá bloqueado até a conclusão da autenticação e das regras de segurança.')

with st.container(border=True):
    st.subheader('Acesso do influenciador')
    st.write('A autenticação individual será habilitada após configurar o Supabase e o endereço público deste portal.')
    st.text_input('E-mail', placeholder='seuemail@exemplo.com', disabled=True)
    st.text_input('Senha', type='password', placeholder='Acesso ainda não disponível', disabled=True)
    st.button('Entrar', disabled=True, type='primary')

st.divider()
a, b, c = st.columns(3)
with a:
    st.metric('Minhas vendas', '—')
with b:
    st.metric('Minhas comissões', '—')
with c:
    st.metric('Saldo a receber', '—')
st.caption('Os indicadores serão exibidos apenas após login e validação de acesso individual.')
