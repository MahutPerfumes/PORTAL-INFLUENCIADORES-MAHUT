import streamlit as st
import pandas as pd
import altair as alt
import requests
import base64
import secrets
import hashlib
import hmac
import time
from urllib.parse import urlencode
import io
import sqlite3
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension
from google.oauth2 import service_account

# =========================================================
# CADASTRO DE DESPESAS MANUAIS — MÓDULO 9
# SQLite local: persiste entre sessões no mesmo servidor, mas o disco
# do Streamlit Community Cloud pode ser apagado em reinicializações.
# Exportar CSV como cópia de segurança até conectar banco externo.
# =========================================================
CATEGORIAS_DESPESAS_MAHUT = (
    "Aluguel", "Combustíveis", "Restaurante e Supermercado",
    "Energia", "Publicidade", "Outras despesas"
)
CAMINHO_DESPESAS_MAHUT = Path("despesas_maHUT.db")


def conectar_despesas_maHUT():
    conexao = sqlite3.connect(str(CAMINHO_DESPESAS_MAHUT), timeout=20)
    conexao.execute("""CREATE TABLE IF NOT EXISTS despesas_maHUT (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        categoria TEXT NOT NULL,
        descricao TEXT NOT NULL,
        valor_centavos INTEGER NOT NULL CHECK(valor_centavos >= 0)
    )""")
    conexao.commit()
    return conexao


def listar_despesas_maHUT(inicio=None, fim=None):
    with conectar_despesas_maHUT() as conexao:
        consulta = "SELECT id, data, categoria, descricao, valor_centavos FROM despesas_maHUT"
        parametros = []
        if inicio is not None and fim is not None:
            consulta += " WHERE data >= ? AND data <= ?"
            parametros = [inicio.isoformat(), fim.isoformat()]
        consulta += " ORDER BY data DESC, categoria, id"
        return [dict(zip(("id", "data", "categoria", "descricao", "valor_centavos"), linha))
                for linha in conexao.execute(consulta, parametros).fetchall()]


def gravar_despesa_maHUT(identificador, data, categoria, descricao, valor):
    if categoria not in CATEGORIAS_DESPESAS_MAHUT:
        raise ValueError("Categoria inválida")
    if not descricao.strip():
        raise ValueError("Informe a descrição da despesa")
    valor = Decimal(str(valor))
    if not valor.is_finite() or valor <= 0:
        raise ValueError("O valor deve ser maior que zero")
    centavos = int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    with conectar_despesas_maHUT() as conexao:
        conexao.execute("""INSERT INTO despesas_maHUT (id, data, categoria, descricao, valor_centavos)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET data=excluded.data, categoria=excluded.categoria,
            descricao=excluded.descricao, valor_centavos=excluded.valor_centavos""",
            (identificador or str(uuid4()), data.isoformat(), categoria, descricao.strip(), centavos))
        conexao.commit()


def excluir_despesa_maHUT(identificador):
    with conectar_despesas_maHUT() as conexao:
        conexao.execute("DELETE FROM despesas_maHUT WHERE id = ?", (identificador,))
        conexao.commit()


def exportar_despesas_maHUT():
    linhas = listar_despesas_maHUT()
    return ("\ufeff" + pd.DataFrame([
        {"id": d["id"], "data": d["data"], "categoria": d["categoria"],
         "descricao": d["descricao"], "valor_centavos": d["valor_centavos"]}
        for d in linhas
    ], columns=["id", "data", "categoria", "descricao", "valor_centavos"]).to_csv(index=False, sep=";"))


def interpretar_data_backup_maHUT(valor):
    """Aceita datas ISO ou brasileiras; armazena sempre em ISO no SQLite."""
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            pass
    raise ValueError(f"Data inválida no backup: {texto!r}. Use DD/MM/AAAA ou AAAA-MM-DD.")


def importar_despesas_maHUT(arquivo):
    """Restaura backup completo sem duplicar registros, em uma transação atômica.

    Identificadores iguais são atualizados apenas se houve mudança; registros
    idênticos com identificadores diferentes também não são duplicados.
    """
    tabela = pd.read_csv(arquivo, sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    colunas = {"id", "data", "categoria", "descricao", "valor_centavos"}
    if not colunas.issubset(tabela.columns):
        raise ValueError("CSV inválido: use o arquivo exportado pelo BI")
    if tabela.empty:
        raise ValueError("O backup está vazio. Nenhum lançamento foi alterado.")
    registros = []
    ids_vistos = set()
    for _, d in tabela.iterrows():
        identificador = str(d["id"]).strip()
        if not identificador or identificador in ids_vistos:
            raise ValueError("O CSV contém ID ausente ou repetido; nenhum dado foi importado")
        ids_vistos.add(identificador)
        dia = interpretar_data_backup_maHUT(d["data"])
        categoria = d["categoria"]
        descricao = d["descricao"].strip()
        if categoria not in CATEGORIAS_DESPESAS_MAHUT or not descricao:
            raise ValueError("CSV contém categoria ou descrição inválida")
        centavos = int(d["valor_centavos"])
        if centavos <= 0:
            raise ValueError("CSV contém valor inválido")
        registros.append((identificador, dia.isoformat(), categoria, descricao, centavos))

    novos = atualizados = existentes = duplicados = 0
    with conectar_despesas_maHUT() as conexao:
        atuais = conexao.execute(
            "SELECT id, data, categoria, descricao, valor_centavos FROM despesas_maHUT"
        ).fetchall()
        por_id = {r[0]: tuple(r[1:]) for r in atuais}
        por_conteudo = {tuple(r[1:]) for r in atuais}
        for identificador, dia, categoria, descricao, centavos in registros:
            conteudo = (dia, categoria, descricao, centavos)
            if identificador in por_id:
                if por_id[identificador] == conteudo:
                    existentes += 1
                else:
                    conexao.execute("""UPDATE despesas_maHUT
                        SET data=?, categoria=?, descricao=?, valor_centavos=? WHERE id=?""",
                        (dia, categoria, descricao, centavos, identificador))
                    por_conteudo.discard(por_id[identificador])
                    por_conteudo.add(conteudo)
                    por_id[identificador] = conteudo
                    atualizados += 1
            elif conteudo in por_conteudo:
                duplicados += 1
            else:
                conexao.execute("""INSERT INTO despesas_maHUT
                    (id, data, categoria, descricao, valor_centavos) VALUES (?, ?, ?, ?, ?)""",
                    (identificador, dia, categoria, descricao, centavos))
                por_id[identificador] = conteudo
                por_conteudo.add(conteudo)
                novos += 1
        conexao.commit()
    return {"novos": novos, "atualizados": atualizados,
            "ja_existentes": existentes, "duplicados_ignorados": duplicados}



# =========================================================
# HISTÓRICO DE CPV POR PEDIDO / ITEM — MÓDULO FINANCEIRO
# Registros locais exigem backup CSV no Streamlit Community Cloud.
# A primeira captura de pedidos antigos usa o custo ATUAL, não
# comprova o custo que vigorava na data histórica da venda.
# =========================================================
def conectar_historico_cpv_maHUT():
    conexao = sqlite3.connect(str(CAMINHO_DESPESAS_MAHUT), timeout=20)
    conexao.execute("""CREATE TABLE IF NOT EXISTS historico_cpv_maHUT (
        pedido TEXT NOT NULL, item_chave TEXT NOT NULL,
        custo_centavos INTEGER NOT NULL CHECK(custo_centavos >= 0),
        origem TEXT NOT NULL, registrado_em TEXT NOT NULL,
        PRIMARY KEY(pedido, item_chave))""")
    conexao.commit()
    return conexao


def chave_item_cpv_maHUT(item):
    variante = item.get("variant") if isinstance(item.get("variant"), dict) else {}
    vid = str(item.get("variant_id") or variante.get("id") or "").strip()
    sku = str(item.get("sku") or "").strip().casefold()
    pid = str(item.get("product_id") or "").strip()
    return ("var:" + vid if vid else "sku:" + sku if sku else "prod:" + pid if pid else "")


def custo_congelado_maHUT(numero, item, custo_catalogo):
    """Congela custo unitário na primeira consulta; nunca sobrescreve histórico."""
    chave = chave_item_cpv_maHUT(item)
    if not chave or custo_catalogo is None:
        return custo_catalogo
    valor = Decimal(str(custo_catalogo))
    if not valor.is_finite() or valor < 0:
        return custo_catalogo
    with conectar_historico_cpv_maHUT() as conexao:
        anterior = conexao.execute(
            "SELECT custo_centavos FROM historico_cpv_maHUT WHERE pedido=? AND item_chave=?",
            (str(numero), chave)).fetchone()
        if anterior is not None:
            return Decimal(anterior[0]) / Decimal(100)
        centavos = int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        conexao.execute("""INSERT OR IGNORE INTO historico_cpv_maHUT
            (pedido, item_chave, custo_centavos, origem, registrado_em)
            VALUES (?, ?, ?, ?, ?)""",
            (str(numero), chave, centavos, "Primeira captura do custo atual do catálogo",
             datetime.now().isoformat(timespec="seconds")))
        conexao.commit()
        return Decimal(centavos) / Decimal(100)


def exportar_historico_cpv_maHUT():
    with conectar_historico_cpv_maHUT() as conexao:
        linhas = conexao.execute("""SELECT pedido, item_chave, custo_centavos, origem,
            registrado_em FROM historico_cpv_maHUT ORDER BY pedido, item_chave""").fetchall()
    return "\ufeff" + pd.DataFrame(linhas, columns=["pedido", "item_chave", "custo_centavos", "origem", "registrado_em"]).to_csv(index=False, sep=";")


def importar_historico_cpv_maHUT(arquivo):
    tabela = pd.read_csv(arquivo, sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    colunas = ["pedido", "item_chave", "custo_centavos", "origem", "registrado_em"]
    if tabela.empty or not set(colunas).issubset(tabela.columns):
        raise ValueError("Arquivo de histórico vazio ou com colunas inválidas")
    registros = []
    vistos = set()
    for _, linha in tabela.iterrows():
        valores = tuple(str(linha[c]).strip() for c in colunas)
        pedido, chave, centavos, origem, registrado = valores
        if not pedido or not chave or not origem or not registrado or not centavos.isdigit():
            raise ValueError("Registro inválido no histórico")
        if (pedido, chave) in vistos:
            raise ValueError("Pedido/item duplicado no arquivo")
        vistos.add((pedido, chave))
        registros.append((pedido, chave, int(centavos), origem, registrado))
    with conectar_historico_cpv_maHUT() as conexao:
        for registro in registros:
            conexao.execute("""INSERT INTO historico_cpv_maHUT VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(pedido, item_chave) DO UPDATE SET
                custo_centavos=excluded.custo_centavos, origem=excluded.origem,
                registrado_em=excluded.registrado_em""", registro)
        conexao.commit()
    return len(registros)

# =========================================================
# RECEITAS MANUAIS — DRE GERENCIAL (MÓDULO 9)
# Mesma política de armazenamento local provisório das despesas.
# =========================================================
CATEGORIAS_RECEITAS_MAHUT = (
    "Venda no showroom", "Venda presencial", "Venda por WhatsApp",
    "Venda em evento", "Outras vendas", "Outras receitas operacionais",
)


def conectar_receitas_maHUT():
    conexao = sqlite3.connect(str(CAMINHO_DESPESAS_MAHUT), timeout=20)
    conexao.execute("""CREATE TABLE IF NOT EXISTS receitas_maHUT (
        id TEXT PRIMARY KEY, data TEXT NOT NULL, categoria TEXT NOT NULL,
        descricao TEXT NOT NULL, valor_centavos INTEGER NOT NULL CHECK(valor_centavos > 0),
        cpv_centavos INTEGER NOT NULL CHECK(cpv_centavos >= 0),
        pedido_referencia TEXT NOT NULL DEFAULT ''
    )""")
    conexao.commit()
    return conexao


def listar_receitas_maHUT(inicio=None, fim=None):
    with conectar_receitas_maHUT() as conexao:
        sql = "SELECT id, data, categoria, descricao, valor_centavos, cpv_centavos, pedido_referencia FROM receitas_maHUT"
        parametros = []
        if inicio is not None and fim is not None:
            sql += " WHERE data >= ? AND data <= ?"
            parametros = [inicio.isoformat(), fim.isoformat()]
        sql += " ORDER BY data DESC, categoria, id"
        return [dict(zip(("id", "data", "categoria", "descricao", "valor_centavos", "cpv_centavos", "pedido_referencia"), r))
                for r in conexao.execute(sql, parametros).fetchall()]


def gravar_receita_maHUT(identificador, dia, categoria, descricao, valor, cpv, referencia=""):
    if categoria not in CATEGORIAS_RECEITAS_MAHUT:
        raise ValueError("Categoria de receita inválida")
    if not descricao.strip():
        raise ValueError("Informe uma descrição")
    valor, cpv = Decimal(str(valor)), Decimal(str(cpv))
    if not valor.is_finite() or not cpv.is_finite() or valor <= 0 or cpv < 0:
        raise ValueError("Receita deve ser positiva e custo não pode ser negativo")
    if categoria != "Outras receitas operacionais" and cpv == 0:
        # Custo zero pode ser verdadeiro; exigir confirmação no formulário.
        pass
    centavos = lambda n: int((n * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    with conectar_receitas_maHUT() as conexao:
        conexao.execute("""INSERT INTO receitas_maHUT
            (id, data, categoria, descricao, valor_centavos, cpv_centavos, pedido_referencia)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET data=excluded.data, categoria=excluded.categoria,
            descricao=excluded.descricao, valor_centavos=excluded.valor_centavos,
            cpv_centavos=excluded.cpv_centavos, pedido_referencia=excluded.pedido_referencia""",
            (identificador or str(uuid4()), dia.isoformat(), categoria, descricao.strip(),
             centavos(valor), centavos(cpv), referencia.strip()))
        conexao.commit()


def excluir_receita_maHUT(identificador):
    with conectar_receitas_maHUT() as conexao:
        conexao.execute("DELETE FROM receitas_maHUT WHERE id = ?", (identificador,))
        conexao.commit()


def exportar_receitas_maHUT():
    colunas = ["id", "data", "categoria", "descricao", "valor_centavos", "cpv_centavos", "pedido_referencia"]
    return "\ufeff" + pd.DataFrame(listar_receitas_maHUT(), columns=colunas).to_csv(index=False, sep=";")


def importar_receitas_maHUT(arquivo):
    tabela = pd.read_csv(arquivo, sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    colunas = {"id", "data", "categoria", "descricao", "valor_centavos", "cpv_centavos", "pedido_referencia"}
    if not colunas.issubset(tabela.columns) or tabela.empty:
        raise ValueError("CSV de receitas inválido ou vazio")
    registros, ids = [], set()
    for _, r in tabela.iterrows():
        identificador = str(r["id"]).strip()
        if not identificador or identificador in ids:
            raise ValueError("ID ausente ou repetido no backup")
        ids.add(identificador)
        dia = interpretar_data_backup_maHUT(r["data"])
        categoria, descricao = str(r["categoria"]), str(r["descricao"]).strip()
        valor, cpv = int(r["valor_centavos"]), int(r["cpv_centavos"])
        if categoria not in CATEGORIAS_RECEITAS_MAHUT or not descricao or valor <= 0 or cpv < 0:
            raise ValueError("Backup contém receita inválida")
        registros.append((identificador, dia.isoformat(), categoria, descricao, valor, cpv,
                          str(r["pedido_referencia"]).strip()))
    novos = atualizados = existentes = duplicados = 0
    with conectar_receitas_maHUT() as conexao:
        atuais = conexao.execute("SELECT id, data, categoria, descricao, valor_centavos, cpv_centavos, pedido_referencia FROM receitas_maHUT").fetchall()
        por_id = {r[0]: tuple(r[1:]) for r in atuais}
        por_conteudo = {tuple(r[1:]) for r in atuais}
        for r in registros:
            identificador, conteudo = r[0], tuple(r[1:])
            if identificador in por_id:
                if por_id[identificador] == conteudo:
                    existentes += 1
                else:
                    conexao.execute("""UPDATE receitas_maHUT SET data=?, categoria=?, descricao=?,
                        valor_centavos=?, cpv_centavos=?, pedido_referencia=? WHERE id=?""", (*conteudo, identificador))
                    por_conteudo.discard(por_id[identificador])
                    por_conteudo.add(conteudo)
                    por_id[identificador] = conteudo
                    atualizados += 1
            elif conteudo in por_conteudo:
                duplicados += 1
            else:
                conexao.execute("INSERT INTO receitas_maHUT VALUES (?, ?, ?, ?, ?, ?, ?)", r)
                por_id[identificador] = conteudo
                por_conteudo.add(conteudo)
                novos += 1
        conexao.commit()
    return {"novos": novos, "atualizados": atualizados, "ja_existentes": existentes,
            "duplicados_ignorados": duplicados}


# =========================================================
# BI MAHUT — VERSÃO CONSOLIDADA
# Fontes reais: GA4 e pedidos da Nuvemshop. Outros módulos demonstrativos.
# =========================================================
st.set_page_config(page_title="BI MAHUT", page_icon="◇", layout="wide", initial_sidebar_state="expanded")



# =========================================================
# MÓDULO 7 — INFLUENCIADORES (DADOS REAIS + SUPABASE)
# =========================================================
def banco_influenciadores():
    import psycopg2
    uri = str(st.secrets.get("supabase", {}).get("database_url", "")).strip()
    if not uri:
        raise ValueError("Conexão Supabase não configurada.")
    return psycopg2.connect(uri, connect_timeout=10, sslmode="require")


def listar_parceiros_maHUT():
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("SELECT id, nome, cupom, comissao_percentual FROM mahut_private.influenciadores_bi ORDER BY nome")
            return cur.fetchall()


def salvar_parceiro_maHUT(nome, email, cupom, percentual):
    nome, cupom, email = nome.strip(), cupom.strip().upper(), email.strip().lower()
    from email.utils import parseaddr
    if not nome or not cupom or not email or parseaddr(email)[1] != email or "@" not in email or not (0 <= percentual <= 100):
        raise ValueError("Informe nome, e-mail válido, cupom e comissão válida.")
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("""INSERT INTO mahut_private.influenciadores_bi(nome,email,cupom,comissao_percentual)
                VALUES (%s,%s,%s,%s) ON CONFLICT(cupom) DO UPDATE
                SET nome=EXCLUDED.nome, email=EXCLUDED.email, comissao_percentual=EXCLUDED.comissao_percentual""",
                (nome, email, cupom, str(percentual)))


def criar_acesso_provisorio_maHUT(email):
    """Cria usuário no Auth sem e-mail de convite; não redefine contas existentes."""
    cfg = st.secrets.get("supabase", {})
    base = str(cfg.get("url", "")).rstrip("/")
    chave = str(cfg.get("service_role_key", "")).strip()
    if not base.startswith("https://") or not chave:
        raise ValueError("Configure supabase.url e supabase.service_role_key nos Secrets do BI.")
    import secrets as gerador_senhas
    senha_temporaria = gerador_senhas.token_urlsafe(15)
    resposta = requests.post(
        f"{base}/auth/v1/admin/users",
        headers={"apikey": chave, "Authorization": f"Bearer {chave}",
                 "Content-Type": "application/json"},
        json={"email": email.strip().lower(), "password": senha_temporaria,
              "email_confirm": True,
              "user_metadata": {"ma_primeiro_acesso": True, "origem": "MAHUT"}},
        timeout=20)
    if resposta.status_code in (400, 409, 422):
        raise ValueError("Conta já existente no Supabase. Não foi alterada; não gere outra senha para este e-mail.")
    if not resposta.ok:
        raise RuntimeError(f"Falha ao criar conta (HTTP {resposta.status_code}). Consulte logs do Supabase.")
    return senha_temporaria


def diagnostico_configuracao_convites_maHUT():
    """Diagnóstico de presença de Secrets, sem mostrar valores confidenciais."""
    try:
        cfg = st.secrets.to_dict()
    except Exception:
        st.error("Não foi possível ler os Secrets deste aplicativo.")
        return None

    admin = cfg.get("portal_admin")
    supa = cfg.get("supabase")
    admin_ok = isinstance(admin, dict) and isinstance(admin.get("password"), str) and bool(admin["password"].strip())
    url_ok = isinstance(supa, dict) and str(supa.get("url", "")).strip().startswith("https://")
    key_ok = isinstance(supa, dict) and bool(str(supa.get("service_role_key", "")).strip())

    if not (admin_ok and url_ok and key_ok):
        st.warning("Criação de acessos indisponível: há configurações ausentes ou em seção incorreta nos Secrets do BI.")
        with st.expander("Diagnóstico seguro da configuração de acessos"):
            st.write("Senha administrativa em [portal_admin]:", "Reconhecida" if admin_ok else "Não reconhecida")
            st.write("URL do projeto em [supabase]:", "Reconhecida" if url_ok else "Não reconhecida")
            st.write("Chave administrativa em [supabase]:", "Reconhecida" if key_ok else "Não reconhecida")
            if not admin_ok:
                st.caption("Confirme que password está abaixo de [portal_admin], no aplicativo BI MAHUT, e não está vazio.")
            if not key_ok:
                st.caption("Confirme que service_role_key está abaixo de [supabase], antes do próximo cabeçalho [seção].")
            st.caption("Nenhum valor secreto é exibido. Não compartilhe suas credenciais.")
    return admin_ok and url_ok and key_ok


def autorizar_convites_maHUT():
    """Exige configurações válidas e senha administrativa antes de enviar convites."""
    if not diagnostico_configuracao_convites_maHUT():
        st.session_state.ma_convites_autorizados = False
        return False
    esperado = str(st.secrets["portal_admin"]["password"])
    if st.session_state.get("ma_convites_autorizados", False):
        if st.button("Encerrar autorização de acessos", key="ma_sair_convites"):
            st.session_state.ma_convites_autorizados = False
            st.rerun()
        return True
    with st.form("ma_autorizar_convites"):
        senha_admin = st.text_input("Senha administrativa para criar acessos", type="password")
        confirmar = st.form_submit_button("Autorizar criação de acessos")
    if confirmar:
        if hmac.compare_digest(senha_admin, esperado):
            st.session_state.ma_convites_autorizados = True
            st.rerun()
        else:
            st.error("Senha administrativa incorreta.")
    return False


def atualizar_status_parceiro_maHUT(parceiro_id, ativo):
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("UPDATE mahut_private.influenciadores_bi SET ativo=%s WHERE id=%s", (bool(ativo), parceiro_id))
            if cur.rowcount != 1:
                raise ValueError("Influenciador não encontrado.")


def listar_status_parceiros_maHUT():
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("SELECT id, ativo FROM mahut_private.influenciadores_bi")
            return {linha[0]: bool(linha[1]) for linha in cur.fetchall()}


def excluir_parceiro_sem_movimentacao_maHUT(parceiro_id):
    """Exclusão administrativa: exige verificação externa do histórico de vendas."""
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM mahut_private.envios_influenciadores_bi WHERE influenciador_id=%s", (parceiro_id,))
            if cur.fetchone()[0]:
                raise ValueError("Há perfumes enviados. Preserve o histórico e desative o influenciador.")
            cur.execute("DELETE FROM mahut_private.influenciadores_bi WHERE id=%s", (parceiro_id,))
            if cur.rowcount != 1:
                raise ValueError("Influenciador não encontrado.")


def listar_envios_maHUT():
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("""SELECT e.id, e.influenciador_id, p.nome, e.data_envio, e.perfume,
                e.sku, e.quantidade, e.custo_unitario
                FROM mahut_private.envios_influenciadores_bi e
                JOIN mahut_private.influenciadores_bi p ON p.id=e.influenciador_id
                ORDER BY e.data_envio DESC, e.id DESC""")
            return cur.fetchall()


def salvar_envio_maHUT(influenciador_id, dia, perfume, sku, quantidade, custo_unitario):
    if not perfume.strip() or quantidade < 1 or custo_unitario < 0:
        raise ValueError("Informe perfume, quantidade e custo válidos.")
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("""INSERT INTO mahut_private.envios_influenciadores_bi
                (influenciador_id,data_envio,perfume,sku,quantidade,custo_unitario)
                VALUES (%s,%s,%s,%s,%s,%s)""",
                (influenciador_id,dia,perfume.strip(),sku.strip(),quantidade,str(custo_unitario)))


def remover_envio_maHUT(envio_id):
    with banco_influenciadores() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM mahut_private.envios_influenciadores_bi WHERE id=%s", (envio_id,))


def cupons_do_pedido_maHUT(pedido):
    """Extrai códigos dos formatos comuns da API sem inferir cupons por desconto."""
    encontrados = set()
    def adicionar(valor):
        if isinstance(valor, str) and valor.strip():
            encontrados.add(valor.strip().upper())
        elif isinstance(valor, dict):
            for campo in ("code", "coupon_code", "coupon", "name"):
                if isinstance(valor.get(campo), str) and valor[campo].strip():
                    encontrados.add(valor[campo].strip().upper())
    for campo in ("coupon", "coupon_code", "coupons", "coupon_codes"):
        dado = pedido.get(campo)
        if isinstance(dado, list):
            for parte in dado:
                adicionar(parte)
        else:
            adicionar(dado)
    return encontrados


def base_comissao_maHUT(pedido):
    """Total pago menos frete cobrado: valor de mercadorias após descontos."""
    total = valor_monetario(pedido.get("total"))
    frete = valor_monetario(pedido.get("shipping_cost_customer"))
    if total is None or frete is None or total < frete:
        return None
    return total - frete


def vendas_influenciadores_maHUT(pedidos, parceiros, inicio, fim):
    por_cupom = {str(p[2]).upper(): p for p in parceiros}
    linhas, ambiguos, sem_base = [], [], []
    vistos = set()
    for pedido in pedidos:
        if not isinstance(pedido, dict):
            continue
        pagamento = str(pedido.get("payment_status") or "").lower().strip()
        situacao = str(pedido.get("status") or "").lower().strip()
        if pagamento != "paid" or situacao in ("cancelled", "canceled", "cancelado"):
            continue
        dia = data_pedido(pedido.get("created_at"))
        if dia is None or not (inicio <= dia <= fim):
            continue
        numero = str(pedido.get("number") or pedido.get("id") or "")
        chave = str(pedido.get("id") or numero)
        if chave in vistos:
            continue
        vistos.add(chave)
        correspondencias = [c for c in cupons_do_pedido_maHUT(pedido) if c in por_cupom]
        if len(correspondencias) > 1:
            ambiguos.append(numero)
            continue
        if not correspondencias:
            continue
        base = base_comissao_maHUT(pedido)
        if base is None:
            sem_base.append(numero)
            continue
        parceiro = por_cupom[correspondencias[0]]
        comissao = base * Decimal(str(parceiro[3])) / Decimal("100")
        linhas.append({"Pedido": numero, "Data": dia, "Influenciador": parceiro[1],
                       "Cupom": parceiro[2], "Faturamento": base,
                       "Comissão": comissao, "ID parceiro": parceiro[0]})
    return linhas, ambiguos, sem_base


def cliente_ga4():
    """Usa os mesmos Secrets da integração GA4 já validada."""
    credenciais_info = dict(st.secrets["ga4_service_account"])
    credenciais = service_account.Credentials.from_service_account_info(credenciais_info)
    cliente = BetaAnalyticsDataClient(credentials=credenciais)
    propriedade = str(st.secrets["ga4"]["property_id"])
    return cliente, propriedade
# =========================================================
# BLING API V3 — AUTORIZAÇÃO OAUTH2 (SOMENTE LEITURA)
# A sessão Streamlit não é armazenamento persistente de tokens.
# =========================================================
BLING_AUTH_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"
BLING_TOKEN_URL = "https://www.bling.com.br/Api/v3/oauth/token"
BLING_API_URL = "https://api.bling.com.br/Api/v3"


def configuracao_bling():
    """Credenciais somente em Secrets, nunca em código ou na interface."""
    bloco = st.secrets.get("bling", {})
    return (str(bloco.get("client_id", "")).strip(),
            str(bloco.get("client_secret", "")).strip(),
            str(bloco.get("redirect_uri", "")).strip())


def trocar_codigo_bling(codigo, client_id, client_secret, redirect_uri):
    autenticacao = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resposta = requests.post(
        BLING_TOKEN_URL,
        headers={"Authorization": f"Basic {autenticacao}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        data={"grant_type": "authorization_code", "code": codigo,
              "redirect_uri": redirect_uri}, timeout=25)
    resposta.raise_for_status()
    dados = resposta.json()
    if not dados.get("access_token"):
        raise ValueError("Bling não retornou token de acesso")
    return dados


def renovar_token_bling(tokens, client_id, client_secret):
    """Renova tokens OAuth em memória, sem expô-los na interface ou logs."""
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise ValueError("Não há refresh token; autorize novamente no Bling.")
    autenticacao = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resposta = requests.post(
        BLING_TOKEN_URL,
        headers={"Authorization": f"Basic {autenticacao}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        timeout=25,
    )
    resposta.raise_for_status()
    novos = resposta.json()
    if not novos.get("access_token"):
        raise ValueError("Bling não retornou novo token de acesso.")
    # Alguns provedores rotacionam o refresh_token: conservar o anterior
    # somente se um novo não tiver sido enviado.
    atualizados = {**tokens, **novos}
    atualizados["obtido_em"] = time.time()
    return atualizados



# =========================================================
# BLING / SUPABASE — TOKENS PERSISTENTES (ITEM 70)
# O segredo de criptografia fica apenas nos Secrets do Streamlit.
# =========================================================
def _bling_fernet():
    from cryptography.fernet import Fernet
    chave = str(st.secrets.get("bling", {}).get("token_encryption_key", "")).strip()
    if not chave:
        raise ValueError("Configure bling.token_encryption_key nos Secrets.")
    return Fernet(chave.encode())


def _bling_banco():
    import psycopg2
    uri = str(st.secrets.get("supabase", {}).get("database_url", "")).strip()
    if not uri:
        raise ValueError("Conexão Supabase não configurada.")
    return psycopg2.connect(uri, connect_timeout=10, sslmode="require")


def _bling_cifrar(tokens):
    import json
    return _bling_fernet().encrypt(json.dumps(tokens).encode()).decode()


def _bling_decifrar(texto):
    import json
    return json.loads(_bling_fernet().decrypt(texto.encode()))


def salvar_tokens_bling(tokens):
    """Grava tokens criptografados, sem expor conteúdo em logs."""
    with _bling_banco() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute("""INSERT INTO mahut_private.bling_oauth_tokens_bi
                (identificador, conteudo_criptografado, atualizado_em)
                VALUES ('principal', %s, NOW())
                ON CONFLICT (identificador) DO UPDATE SET
                conteudo_criptografado=EXCLUDED.conteudo_criptografado,
                atualizado_em=NOW()""", (_bling_cifrar(tokens),))


def carregar_tokens_bling():
    with _bling_banco() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute("""SELECT conteudo_criptografado
                FROM mahut_private.bling_oauth_tokens_bi
                WHERE identificador='principal'""")
            linha = cursor.fetchone()
    return _bling_decifrar(linha[0]) if linha else None


def renovar_tokens_bling_persistidos(forcar=False, token_antigo=None):
    """Bloqueio transacional evita consumir o mesmo refresh token simultaneamente."""
    with _bling_banco() as conexao:
        with conexao.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(72190620)")
            cursor.execute("""SELECT conteudo_criptografado
                FROM mahut_private.bling_oauth_tokens_bi
                WHERE identificador='principal' FOR UPDATE""")
            linha = cursor.fetchone()
            if not linha:
                raise ValueError("Autorize novamente o Bling em Configurações.")
            tokens = _bling_decifrar(linha[0])
            if forcar and token_antigo and tokens.get("access_token") != token_antigo:
                return tokens  # Outra sessão já renovou.
            validade = float(tokens.get("obtido_em", 0)) + int(tokens.get("expires_in", 3600))
            if not forcar and time.time() < validade - 120:
                return tokens
            client_id, client_secret, _ = configuracao_bling()
            novos = renovar_token_bling(tokens, client_id, client_secret)
            cursor.execute("""UPDATE mahut_private.bling_oauth_tokens_bi
                SET conteudo_criptografado=%s, atualizado_em=NOW()
                WHERE identificador='principal'""", (_bling_cifrar(novos),))
            return novos


def tokens_bling_ativos():
    tokens = carregar_tokens_bling()
    if not tokens:
        return None
    validade = float(tokens.get("obtido_em", 0)) + int(tokens.get("expires_in", 3600))
    if time.time() >= validade - 120:
        tokens = renovar_tokens_bling_persistidos()
    st.session_state["bling_tokens"] = tokens
    return tokens

def consultar_bling_sessao(caminho):
    """Consulta de leitura, com persistência e renovação segura após 401."""
    tokens = tokens_bling_ativos()
    if not tokens:
        raise ValueError("Autorize o Bling em Configurações antes de consultar.")
    try:
        return consultar_bling_leitura(caminho, tokens["access_token"])
    except requests.HTTPError as erro:
        if erro.response is None or erro.response.status_code != 401:
            raise
        novos = renovar_tokens_bling_persistidos(
            forcar=True, token_antigo=tokens["access_token"])
        st.session_state["bling_tokens"] = novos
        return consultar_bling_leitura(caminho, novos["access_token"])


def consultar_bling_leitura(caminho, access_token):
    resposta = requests.get(
        f"{BLING_API_URL}/{caminho.lstrip('/')}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=25)
    resposta.raise_for_status()
    return resposta.json()


def gerar_estado_bling(client_secret):
    """State assinado, válido por 10 minutos, independente de session_state."""
    conteudo = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    assinatura = hmac.new(client_secret.encode(), conteudo.encode(), hashlib.sha256).hexdigest()
    return f"{conteudo}.{assinatura}"


def validar_estado_bling(estado, client_secret):
    try:
        timestamp, nonce, assinatura = str(estado).split(".", 2)
        idade = time.time() - int(timestamp)
        if not (0 <= idade <= 600) or not nonce:
            return False
        conteudo = f"{timestamp}.{nonce}"
        esperado = hmac.new(client_secret.encode(), conteudo.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(assinatura, esperado)
    except (ValueError, TypeError, OverflowError):
        return False


def painel_autenticacao_bling():
    st.subheader("Conexão Bling API v3")
    st.caption("Autorização privada para consultas de leitura. Não altera pedidos, produtos ou estoque.")
    client_id, client_secret, redirect_uri = configuracao_bling()
    if not all((client_id, client_secret, redirect_uri)):
        st.warning("Configure [bling] com client_id, client_secret e redirect_uri nos Secrets do Streamlit.")
        return
    if not redirect_uri.startswith("https://"):
        st.warning("A URL de redirecionamento precisa usar HTTPS e coincidir exatamente com a cadastrada no Bling.")
        return
    params = st.query_params
    erro = params.get("error")
    codigo = params.get("code")
    estado_recebido = params.get("state")
    if erro:
        st.error("O Bling não autorizou o acesso. Confira as permissões e tente novamente.")
    if codigo:
        if not validar_estado_bling(estado_recebido, client_secret):
            st.error("Validação de segurança da autorização falhou. Inicie novamente pelo botão abaixo, na mesma sessão.")
        elif st.session_state.get("bling_oauth_code_usado") != codigo:
            try:
                st.session_state["bling_tokens"] = trocar_codigo_bling(codigo, client_id, client_secret, redirect_uri)
                st.session_state["bling_tokens"]["obtido_em"] = time.time()
                salvar_tokens_bling(st.session_state["bling_tokens"])
                st.session_state["bling_oauth_code_usado"] = codigo
                st.success("Bling autorizado e tokens protegidos no Supabase!")
                st.query_params.clear()
            except requests.HTTPError as exc:
                st.error(f"Bling recusou a troca do código (HTTP {exc.response.status_code if exc.response is not None else 'desconhecido'}). Confira a URL de retorno e as credenciais.")
            except Exception:
                st.error("Falha na autorização do Bling. Reinicie o processo e confira a configuração.")
    if "bling_tokens" not in st.session_state:
        try:
            tokens_bling_ativos()
        except Exception:
            st.warning("Não foi possível recuperar tokens persistidos. Confira Supabase e chave de criptografia.")
    if "bling_tokens" not in st.session_state:
        if st.button("Preparar autorização Bling", key="bling_preparar"):
            st.session_state["bling_oauth_state"] = gerar_estado_bling(client_secret)
        if st.session_state.get("bling_oauth_state"):
            parametros = {"response_type": "code", "client_id": client_id,
                          "redirect_uri": redirect_uri,
                          "state": st.session_state["bling_oauth_state"]}
            st.link_button("Autorizar no Bling", BLING_AUTH_URL + "?" + urlencode(parametros))
            st.caption("Após autorizar, volte a esta sessão. O retorno precisa apontar para a URL exata configurada no aplicativo Bling.")
    else:
        st.success("Bling conectado com tokens persistidos no Supabase.")
        if st.button("Testar persistência dos tokens", key="bling_teste_persistencia"):
            try:
                tokens_salvos = carregar_tokens_bling()
                if tokens_salvos and tokens_salvos.get("access_token") and tokens_salvos.get("refresh_token"):
                    st.success("Tokens criptografados recuperados do Supabase com sucesso.")
                else:
                    st.warning("Não existem tokens completos armazenados. Autorize o Bling novamente.")
            except Exception:
                st.error("Falha na leitura persistente. Confira Supabase e chave de criptografia.")
        if st.button("Testar leitura de produtos no Bling", key="bling_teste_produtos"):
            try:
                dados = consultar_bling_sessao("produtos?pagina=1&limite=1")
                if isinstance(dados, dict) and "data" in dados:
                    st.success("Conexão Bling API v3 validada com sucesso (consulta somente leitura).")
                else:
                    st.warning("Bling respondeu, mas em formato inesperado.")
            except requests.HTTPError as exc:
                st.error(f"Consulta Bling recusada (HTTP {exc.response.status_code if exc.response is not None else 'desconhecido'}). Verifique o escopo Produtos e a validade do token.")
            except Exception:
                st.error("Não foi possível consultar o Bling. Confira a conexão e tente novamente.")
        if st.button("Testar leitura de pedidos de venda no Bling", key="bling_teste_pedidos"):
            try:
                dados = consultar_bling_sessao("pedidos/vendas?pagina=1&limite=10")
                pedidos = dados.get("data") if isinstance(dados, dict) else None
                if isinstance(pedidos, list):
                    st.success(f"Consulta de pedidos do Bling validada: {len(pedidos)} registro(s) nesta página.")
                    resumo = []
                    for pedido in pedidos:
                        if not isinstance(pedido, dict):
                            continue
                        resumo.append({
                            "Número Bling": pedido.get("numero"),
                            "Data": pedido.get("data"),
                            "Número loja virtual": pedido.get("numeroLojaVirtual") or "",
                            "Total (R$)": pedido.get("total"),
                        })
                    if resumo:
                        st.dataframe(pd.DataFrame(resumo), hide_index=True, use_container_width=True)
                    st.caption("Somente leitura. Estes pedidos ainda NÃO entram no faturamento do BI; a conciliação com a Nuvemshop será feita antes.")
                else:
                    st.warning("Bling respondeu com um formato de pedidos inesperado.")
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "desconhecido"
                st.error(f"Consulta de pedidos recusada pelo Bling (HTTP {status}).")
            except Exception:
                st.error("Não foi possível consultar pedidos do Bling. Confira a autorização e tente novamente.")
        if st.button("Encerrar autorização nesta sessão", key="bling_desconectar"):
            st.session_state.pop("bling_tokens", None)
            st.session_state.pop("bling_oauth_state", None)
            st.rerun()
    st.info("Tokens criptografados no Supabase. 'Encerrar autorização nesta sessão' não revoga o aplicativo no Bling nem apaga os tokens persistidos.")


# =========================================================
# SUPABASE — TESTE DE CONEXÃO POSTGRESQL (SOMENTE LEITURA)
# A URI permanece exclusivamente nos Secrets do Streamlit.
# =========================================================
def testar_conexao_supabase_maHUT():
    uri = str(st.secrets.get("supabase", {}).get("database_url", "")).strip()
    if not uri:
        return False, "Não foi encontrada a seção [supabase] com database_url nos Secrets."
    if not uri.startswith(("postgresql://", "postgres://")):
        return False, "A URI precisa começar com postgresql:// ou postgres://."
    try:
        import psycopg2
    except ImportError:
        return False, "Dependência ausente: adicione psycopg2-binary ao requirements.txt e reinicie o aplicativo."
    try:
        # SSL obrigatório. Sem imprimir URI, usuário, senha ou detalhes internos.
        with psycopg2.connect(uri, connect_timeout=10, sslmode="require") as conexao:
            conexao.set_session(readonly=True)
            with conexao.cursor() as cursor:
                cursor.execute("SELECT 1, to_regclass('mahut_private.bling_oauth_tokens')")
                teste, tabela = cursor.fetchone()
        if teste != 1:
            return False, "O servidor respondeu de forma inesperada."
        if tabela is None:
            return False, "Conexão estabelecida, mas a tabela privada do Bling não foi localizada."
        return True, "Conexão Supabase validada! Tabela privada do Bling encontrada (somente leitura)."
    except psycopg2.Error as erro:
        # Não exibir mensagens brutas do driver: podem conter dados da URI.
        codigo = getattr(erro, "pgcode", None)
        if codigo == "28P01":
            return False, "Senha do banco recusada. Confira a URI nos Secrets, sem compartilhá-la."
        return False, "Não foi possível conectar ao Supabase. Confira Session pooler, URI, senha e rede."
    except Exception:
        return False, "Falha ao testar a conexão. Nenhuma informação confidencial foi exibida."


def testar_conexao_nuvemshop():
    try:
        loja = st.secrets["nuvemshop"]["store_id"]
        token = st.secrets["nuvemshop"]["access_token"]

        url = f"https://api.nuvemshop.com.br/v1/{loja}/orders"

        headers = {
            "Authentication": f"bearer {token}",
            "User-Agent": "BI MAHUT (contato@mahut.ind.br)"
        }

        resposta = requests.get(
            url,
            headers=headers,
            params={"per_page": 1},
            timeout=20
        )

        if resposta.status_code == 200:
            return True, "Conexão com a Nuvemshop realizada com sucesso!"

        return False, f"Erro HTTP {resposta.status_code} na conexão."

    except Exception:
        return False, "Não foi possível conectar. Verifique as configurações."
@st.cache_data(ttl=300, show_spinner=False)
def consultar_pedidos_nuvemshop():
    loja = st.secrets["nuvemshop"]["store_id"]
    token = st.secrets["nuvemshop"]["access_token"]

    url = f"https://api.nuvemshop.com.br/v1/{loja}/orders"

    headers = {
        "Authentication": f"bearer {token}",
        "User-Agent": "BI MAHUT (contato@mahut.ind.br)"
    }

    pedidos = []
    pagina_api = 1

    while True:
        resposta = requests.get(
            url,
            headers=headers,
            params={"page": pagina_api, "per_page": 200},
            timeout=30
        )
        resposta.raise_for_status()

        lote = resposta.json()

        if not isinstance(lote, list):
            raise ValueError("Resposta inesperada da Nuvemshop.")

        pedidos.extend(lote)

        if len(lote) < 200:
            break

        pagina_api += 1

    return pedidos


@st.cache_data(ttl=300, show_spinner=False)
def consultar_produtos_nuvemshop_diagnostico():
    """Consulta paginada, somente leitura, dos produtos e custos da loja."""
    loja = st.secrets["nuvemshop"]["store_id"]
    token = st.secrets["nuvemshop"]["access_token"]
    url = f"https://api.nuvemshop.com.br/v1/{loja}/products"
    headers = {
        "Authentication": f"bearer {token}",
        "User-Agent": "BI MAHUT (contato@mahut.ind.br)"
    }
    produtos = []
    pagina_api = 1
    while True:
        resposta = requests.get(url, headers=headers,
                                params={"page": pagina_api, "per_page": 200}, timeout=30)
        resposta.raise_for_status()
        lote = resposta.json()
        if not isinstance(lote, list):
            raise ValueError("Formato inesperado na consulta de produtos.")
        produtos.extend(lote)
        if len(lote) < 200:
            break
        pagina_api += 1
    return produtos


def nome_produto_diagnostico(produto):
    nome = produto.get("name") or ""
    if isinstance(nome, dict):
        return str(nome.get("pt") or nome.get("pt_BR") or next(iter(nome.values()), ""))
    return str(nome)


def linhas_custo_diagnostico(produtos):
    linhas = []
    for produto in produtos:
        nome = nome_produto_diagnostico(produto)
        variantes = produto.get("variants") or []
        for variante in variantes:
            if not isinstance(variante, dict):
                continue
            linhas.append({
                "Produto": nome,
                "ID do produto": produto.get("id"),
                "ID da variação": variante.get("id"),
                "SKU": variante.get("sku") or "Não informado",
                "Preço (API)": variante.get("price"),
                "Custo (API)": variante.get("cost"),
                "Campo 'cost' presente?": "Sim" if "cost" in variante else "Não",
                "Campo 'cost_price' presente?": "Sim" if "cost_price" in variante else "Não",
                "Valor de 'cost_price'": variante.get("cost_price"),
            })
    return linhas



# =========================================================
# MÓDULO 4 — ESTOQUE DA LOJA VIRTUAL (NUVEMSHOP)
# =========================================================
def linhas_estoque_nuvemshop(catalogo):
    """Saldos e custos atuais por variação, sem inventar estoque ausente."""
    registros = []
    for produto in catalogo:
        if not isinstance(produto, dict):
            continue
        nome = nome_produto_diagnostico(produto)
        for variante in produto.get("variants") or []:
            if not isinstance(variante, dict):
                continue
            sku = str(variante.get("sku") or "").strip()
            controle = variante.get("stock_management", produto.get("stock_management"))
            bruto = variante.get("stock")
            saldo = valor_monetario(bruto)
            # Stock management desativado significa saldo não controlado.
            controlado = controle is not False
            saldo_valido = (controlado and saldo is not None and saldo >= 0
                            and saldo == saldo.to_integral_value())
            custo = valor_monetario(variante.get("cost"))
            custo_valido = custo is not None and custo >= 0
            registros.append({
                "Produto": nome,
                "SKU": sku or "Não informado",
                "ID variação": str(variante.get("id") or ""),
                "Saldo": int(saldo) if saldo_valido else None,
                "Custo unitário": custo if custo_valido else None,
                "Capital": int(saldo) * custo if saldo_valido and custo_valido else None,
                "Situação": ("Sem controle de estoque" if not controlado else
                             "Saldo indisponível" if not saldo_valido else
                             "Zerado" if saldo == 0 else "Disponível"),
            })
    return registros


def movimento_estoque_por_variacao(pedidos, inicio, fim):
    """Unidades de pedidos pagos, sem cancelados/reembolsados; não é baixa física total."""
    por_variacao, por_sku = {}, {}
    pedidos_validos = 0
    itens_sem_chave = 0
    for pedido in pedidos:
        data = data_pedido(pedido.get("created_at"))
        pagamento = str(pedido.get("payment_status") or "").strip().lower()
        situacao = str(pedido.get("status") or "").strip().lower()
        if (data is None or not inicio <= data <= fim or pagamento != "paid"
                or situacao in ("cancelled", "canceled", "cancelado")):
            continue
        pedidos_validos += 1
        for item in pedido.get("products") or []:
            if not isinstance(item, dict):
                continue
            quantidade = valor_monetario(item.get("quantity"))
            if quantidade is None or quantidade <= 0:
                continue
            variante = item.get("variant") if isinstance(item.get("variant"), dict) else {}
            vid = str(item.get("variant_id") or variante.get("id") or "").strip()
            sku = str(item.get("sku") or "").strip().casefold()
            if vid:
                por_variacao[vid] = por_variacao.get(vid, Decimal("0")) + quantidade
            elif sku:
                por_sku[sku] = por_sku.get(sku, Decimal("0")) + quantidade
            else:
                itens_sem_chave += 1
    return por_variacao, por_sku, pedidos_validos, itens_sem_chave


def analise_giro_estoque(registros, pedidos, inicio, fim):
    """Cobertura indicativa baseada SOMENTE em vendas pagas da loja virtual."""
    por_variacao, por_sku, n_pedidos, sem_chave = movimento_estoque_por_variacao(pedidos, inicio, fim)
    dias = (fim - inicio).days + 1
    linhas = []
    for r in registros:
        vid = r["ID variação"]
        sku = r["SKU"].strip().casefold()
        # Quando a API do pedido traz ID da variação, priorizá-lo sobre SKU.
        vendido = por_variacao.get(vid, Decimal("0")) if vid else Decimal("0")
        if sku and sku != "não informado":
            vendido += por_sku.get(sku, Decimal("0"))
        saldo = r["Saldo"]
        media_dia = vendido / Decimal(dias)
        cobertura = (Decimal(saldo) / media_dia if saldo is not None and media_dia > 0 else None)
        if saldo is None:
            leitura = "Saldo pendente"
        elif vendido == 0:
            leitura = "Sem vendas no período — histórico insuficiente para concluir"
        elif dias < 30:
            leitura = "Sinal inicial — não recomendar reposição automática"
        else:
            leitura = "Tendência da loja virtual — conferir outros canais"
        linhas.append({"Produto": r["Produto"], "SKU": r["SKU"],
                       "Saldo": saldo, "Unidades vendidas": vendido,
                       "Média diária": media_dia, "Cobertura": cobertura,
                       "Capital": r["Capital"], "Leitura": leitura})
    return linhas, n_pedidos, sem_chave


def indice_custos_catalogo(produtos):
    """Índices de custo por variação, produto e SKU, sem valores fixos."""
    por_variacao, por_produto, por_sku = {}, {}, {}
    for produto in produtos:
        pid = str(produto.get("id") or "")
        variantes = produto.get("variants") or []
        for variante in variantes:
            if not isinstance(variante, dict):
                continue
            custo = valor_monetario(variante.get("cost"))
            if custo is None or custo < 0:
                continue
            registro = {"custo": custo, "produto": nome_produto_diagnostico(produto),
                        "sku": str(variante.get("sku") or "").strip()}
            vid = str(variante.get("id") or "")
            if vid:
                por_variacao[vid] = registro
            if pid:
                por_produto.setdefault(pid, []).append(registro)
            if registro["sku"]:
                por_sku.setdefault(registro["sku"].casefold(), []).append(registro)
    return por_variacao, por_produto, por_sku


def cpv_pedidos(pedidos, produtos):
    """Calcula CPV somente para pedidos pagos não cancelados; sinaliza itens não conciliados."""
    por_variacao, por_produto, por_sku = indice_custos_catalogo(produtos)
    linhas, pendencias = [], []
    for pedido in pedidos:
        pagamento = str(pedido.get("payment_status") or "").lower().strip()
        status = str(pedido.get("status") or "").lower().strip()
        if pagamento != "paid" or status in ("cancelled", "canceled", "cancelado"):
            continue
        numero = str(pedido.get("number") or pedido.get("id") or "—")
        data = data_pedido(pedido.get("created_at"))
        total = valor_monetario(pedido.get("total"))
        itens = pedido.get("products")
        if not isinstance(itens, list) or not itens:
            pendencias.append({"Pedido": numero, "Item": "Itens não retornados pela API", "Motivo": "Pedido sem lista de produtos"})
            linhas.append({"Pedido": numero, "Data": data, "CPV": None, "Itens": 0, "Completo": False, "Total": total})
            continue
        cpv = Decimal("0")
        completo = True
        for item in itens:
            if not isinstance(item, dict):
                completo = False
                pendencias.append({"Pedido": numero, "Item": "Item inválido", "Motivo": "Formato inesperado"})
                continue
            variante_item = item.get("variant") if isinstance(item.get("variant"), dict) else {}
            vid = str(item.get("variant_id") or variante_item.get("id") or "")
            pid = str(item.get("product_id") or "")
            sku = str(item.get("sku") or "").strip().casefold()
            registro = por_variacao.get(vid)
            if registro is None and sku:
                candidatos = por_sku.get(sku, [])
                if len(candidatos) == 1:
                    registro = candidatos[0]
            if registro is None and pid:
                candidatos = por_produto.get(pid, [])
                if len(candidatos) == 1:
                    registro = candidatos[0]
            qtd = valor_monetario(item.get("quantity"))
            if registro is None or qtd is None or qtd <= 0:
                completo = False
                motivo = "Custo ou identificação não encontrado" if registro is None else "Quantidade inválida"
                pendencias.append({"Pedido": numero, "Item": str(item.get("name") or sku or pid or vid or "Sem identificação"), "Motivo": motivo})
                continue
            cpv += custo_congelado_maHUT(numero, item, registro["custo"]) * qtd
        linhas.append({"Pedido": numero, "Data": data, "CPV": cpv if completo else None,
                       "Itens": len(itens), "Completo": completo, "Total": total})
    return linhas, pendencias


# =========================================================
# MÓDULO 3 — RENTABILIDADE POR PRODUTO E POR VENDA
# =========================================================
def linhas_rentabilidade_produtos(pedidos, catalogo, taxas_reais):
    """Rateio gerencial: receita de mercadorias e taxas por peso do item.
    Não inclui frete na receita do produto nem atribui custo de envio ao item.
    """
    por_variacao, por_produto, por_sku = indice_custos_catalogo(catalogo)
    linhas, alertas = [], []
    for pedido in pedidos:
        if str(pedido.get("payment_status") or "").lower() != "paid" or str(pedido.get("status") or "").lower() in ("cancelled", "canceled", "cancelado"):
            continue
        numero = str(pedido.get("number") or pedido.get("id") or "—")
        dia = data_pedido(pedido.get("created_at"))
        total = valor_monetario(pedido.get("total"))
        frete = valor_monetario(pedido.get("shipping_cost_customer"))
        itens = pedido.get("products") or []
        if total is None or frete is None or total < frete or not isinstance(itens, list) or not itens:
            alertas.append(f"Pedido #{numero}: total, frete ou itens indisponíveis; excluído da rentabilidade.")
            continue
        preparados = []
        for item in itens:
            if not isinstance(item, dict):
                preparados = []
                break
            qtd = valor_monetario(item.get("quantity"))
            preco = valor_monetario(item.get("price"))
            if qtd is None or qtd <= 0 or preco is None or preco < 0:
                preparados = []
                break
            variante = item.get("variant") if isinstance(item.get("variant"), dict) else {}
            vid = str(item.get("variant_id") or variante.get("id") or "")
            pid = str(item.get("product_id") or "")
            sku = str(item.get("sku") or "").strip().casefold()
            reg = por_variacao.get(vid)
            if reg is None and sku and len(por_sku.get(sku, [])) == 1:
                reg = por_sku[sku][0]
            if reg is None and pid and len(por_produto.get(pid, [])) == 1:
                reg = por_produto[pid][0]
            nome = item.get("name") or (reg["produto"] if reg else "Produto sem nome")
            if isinstance(nome, dict):
                nome = nome.get("pt") or nome.get("pt_BR") or next(iter(nome.values()), "Produto sem nome")
            preparados.append({"nome": str(nome), "sku": sku.upper(), "qtd": qtd,
                               "peso": preco * qtd, "custo": custo_congelado_maHUT(numero, item, reg["custo"]) * qtd if reg else None})
        if not preparados:
            alertas.append(f"Pedido #{numero}: itens sem preço ou quantidade válida; excluído.")
            continue
        soma_pesos = sum((i["peso"] for i in preparados), Decimal("0"))
        if soma_pesos <= 0:
            alertas.append(f"Pedido #{numero}: itens sem valor para rateio; excluído.")
            continue
        receita_produtos = total - frete
        taxa_real = decimal_br(taxas_reais.get(numero, ""))
        taxa_estimada, _, _ = taxa_pagamento_estimada(pedido)
        taxa_pedido = taxa_real if taxa_real is not None else taxa_estimada
        for i in preparados:
            proporcao = i["peso"] / soma_pesos
            receita = receita_produtos * proporcao
            imposto = receita * Decimal("0.04")
            taxa = taxa_pedido * proporcao if taxa_pedido is not None else None
            contribuicao = receita - imposto - i["custo"] - taxa if i["custo"] is not None and taxa is not None else None
            margem_item = (contribuicao / receita * Decimal("100")
                           if contribuicao is not None and receita > 0 else None)
            linhas.append({"Pedido": numero, "Data": dia, "Produto": i["nome"], "SKU": i["sku"],
                           "Unidades": i["qtd"], "Receita": receita, "CPV": i["custo"],
                           "Imposto": imposto, "Taxa": taxa, "Contribuição": contribuicao,
                           "Margem": margem_item})
            if i["custo"] is None:
                alertas.append(f"Pedido #{numero} — {i['nome']}: CPV não encontrado no catálogo.")
            if taxa is None:
                alertas.append(f"Pedido #{numero}: taxa de pagamento não disponível.")
    return linhas, list(dict.fromkeys(alertas))


def tabela_rentabilidade_produtos(linhas):
    grupos = {}
    for linha in linhas:
        chave = (linha["Produto"], linha["SKU"])
        if chave not in grupos:
            grupos[chave] = {"Produto": chave[0], "SKU": chave[1], "Unidades": Decimal("0"),
                             "Pedidos": set(), "Receita": Decimal("0"), "CPV": Decimal("0"),
                             "Imposto": Decimal("0"), "Taxa": Decimal("0"), "Contribuição": Decimal("0"),
                             "Completo": True}
        g = grupos[chave]
        g["Unidades"] += linha["Unidades"]
        g["Pedidos"].add(linha["Pedido"])
        g["Receita"] += linha["Receita"]
        g["Imposto"] += linha["Imposto"]
        for campo in ("CPV", "Taxa", "Contribuição"):
            if linha[campo] is None:
                g["Completo"] = False
            else:
                g[campo] += linha[campo]
    saida = []
    for g in grupos.values():
        g["Pedidos"] = len(g["Pedidos"])
        if not g["Completo"]:
            for campo in ("CPV", "Taxa", "Contribuição"):
                g[campo] = None
        g["Margem"] = g["Contribuição"] / g["Receita"] * 100 if g["Contribuição"] is not None and g["Receita"] else None
        saida.append(g)
    return sorted(saida, key=lambda x: x["Receita"], reverse=True)


def exibir_tabela_rentabilidade(registros, incluir_pedido=False):
    colunas = (["Pedido", "Data"] if incluir_pedido else []) + ["Produto", "SKU", "Unidades"]
    if not incluir_pedido:
        colunas.append("Pedidos")
    colunas += ["Receita", "CPV", "Imposto", "Taxa", "Contribuição"]
    # Exibe a margem percentual tanto no consolidado quanto venda a venda.
    colunas.append("Margem")
    saida = []
    for r in registros:
        linha = {}
        for c in colunas:
            v = r.get(c)
            if c in ("Receita", "CPV", "Imposto", "Taxa", "Contribuição"):
                linha[c + " (R$)"] = moeda(v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if v is not None else "Pendente"
            elif c == "Margem":
                linha["Margem (%)"] = f"{v:.2f}%".replace(".", ",") if v is not None else "Pendente"
            elif c == "Unidades":
                linha[c] = float(v)
            else:
                linha[c] = v
        saida.append(linha)
    tabela_br(pd.DataFrame(saida), use_container_width=True, hide_index=True)

def valor_monetario(valor):
    """Converte valores numéricos da API sem arredondamento binário."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = Decimal(str(valor).strip())
        return numero if numero.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def data_pedido(valor):
    """Converte data ISO da Nuvemshop para data civil, sem deslocar o dia."""
    if not valor:
        return None
    try:
        return datetime.strptime(str(valor)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def consolidar_vendas(pedidos):
    """Consolida a situação dos pedidos, sem somar cancelamentos à receita."""
    linhas = []
    for pedido in pedidos:
        pagamento = str(pedido.get("payment_status") or "").strip().lower()
        situacao = str(pedido.get("status") or "").strip().lower()
        valor = valor_monetario(pedido.get("total"))
        frete = valor_monetario(pedido.get("shipping_cost_customer"))
        subtotal = valor_monetario(pedido.get("subtotal"))
        desconto = valor_monetario(pedido.get("discount"))
        if situacao in ("cancelled", "canceled", "cancelado") or pagamento == "voided":
            grupo = "Cancelado"
        elif pagamento in ("refunded", "partially_refunded"):
            grupo = "Reembolsado / parcial"
        elif pagamento == "paid":
            grupo = "Pago"
        elif pagamento == "pending":
            grupo = "Pendente"
        else:
            grupo = "Outro status"
        linhas.append({
            "Pedido": str(pedido.get("number") or pedido.get("id") or "—"),
            "Data": data_pedido(pedido.get("created_at")),
            "Pagamento": grupo,
            "Valor do pedido (R$)": float(valor) if valor is not None else None,
            "Frete cobrado (R$)": float(frete) if frete is not None else None,
            "Subtotal (R$)": float(subtotal) if subtotal is not None else None,
            "Desconto informado (R$)": float(desconto) if desconto is not None else None,
            "_total": valor,
            "_frete": frete,
        })
    return pd.DataFrame(linhas, columns=[
        "Pedido", "Data", "Pagamento", "Valor do pedido (R$)",
        "Frete cobrado (R$)", "Subtotal (R$)", "Desconto informado (R$)",
        "_total", "_frete",
    ])


def indicadores_vendas(tabela):
    contagens = {nome: int((tabela["Pagamento"] == nome).sum()) for nome in
                 ("Pago", "Pendente", "Cancelado", "Reembolsado / parcial", "Outro status")}
    pagos = tabela[tabela["Pagamento"] == "Pago"]
    pendentes = tabela[tabela["Pagamento"] == "Pendente"]
    def soma_validos(valores):
        return sum((v for v in valores if v is not None), Decimal("0"))
    confirmado = soma_validos(pagos["_total"])
    pendente = soma_validos(pendentes["_total"])
    frete_conhecido = soma_validos(pagos["_frete"])
    sem_frete = int(pagos["_frete"].isna().sum())
    sem_total = int(tabela["_total"].isna().sum())
    return contagens, confirmado, pendente, frete_conhecido, sem_frete, sem_total


@st.cache_data(ttl=900, show_spinner=False)
def consultar_ga4_canais():
    cliente, propriedade = cliente_ga4()
    requisicao = RunReportRequest(
        property=f"properties/{propriedade}",
        dimensions=[Dimension(name="sessionDefaultChannelGroup")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
        limit=100,
    )
    resposta = cliente.run_report(requisicao)
    dados = [{"Canal": linha.dimension_values[0].value,
              "Sessões": int(linha.metric_values[0].value)} for linha in resposta.rows]
    if not dados:
        return pd.DataFrame(columns=["Canal", "Sessões"])
    return pd.DataFrame(dados).sort_values("Sessões", ascending=False)


@st.cache_data(ttl=900, show_spinner=False)
def consultar_ga4_sessoes():
    cliente, propriedade = cliente_ga4()
    requisicao = RunReportRequest(
        property=f"properties/{propriedade}",
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
    )
    resposta = cliente.run_report(requisicao)
    return int(resposta.rows[0].metric_values[0].value) if resposta.rows else 0


@st.cache_data(ttl=900, show_spinner=False)
def consultar_ga4_evolucao_diaria():
    """Sessões por dia nos mesmos 7 dias completos usados nos cartões."""
    cliente, propriedade = cliente_ga4()
    requisicao = RunReportRequest(
        property=f"properties/{propriedade}",
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="yesterday")],
        limit=100,
    )
    resposta = cliente.run_report(requisicao)
    dados = [
        {"Data": pd.to_datetime(linha.dimension_values[0].value, format="%Y%m%d"),
         "Sessões": int(linha.metric_values[0].value)}
        for linha in resposta.rows
    ]
    if not dados:
        return pd.DataFrame(columns=["Data", "Sessões"])
    tabela = pd.DataFrame(dados).sort_values("Data")
    # Mostra também dias sem sessões, se houver.
    datas = pd.date_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=7)
    return (tabela.set_index("Data").reindex(datas, fill_value=0)
            .rename_axis("Data").reset_index())


def testar_conexao_ga4():
    try:
        return True, consultar_ga4_sessoes()
    except Exception as erro:
        return False, str(erro)


st.markdown("""
<style>
.stApp { background-color:#F6F2EA; color:#1C1C1C; }
[data-testid="stSidebar"] { background-color:#111111; }
[data-testid="stSidebar"] * { color:#F5F1E8; }
[data-testid="stSidebar"] [role="radiogroup"] label { padding:8px 10px; border-radius:8px; }
[data-testid="stSidebar"] [role="radiogroup"] label:hover { background-color:#252525; }
.block-container { padding-top:2rem; padding-bottom:3rem; max-width:1450px; }
.mahut-title { font-size:34px; font-weight:700; letter-spacing:5px; color:#171717; margin-bottom:0; }
.mahut-sub { color:#777; font-size:14px; margin-top:-5px; margin-bottom:25px; }
.card { background:#FFF; border:1px solid #E4DED3; border-radius:14px; padding:20px 22px; min-height:118px; box-shadow:0 2px 8px rgba(0,0,0,.04); }
.kpi-label { font-size:12px; color:#777; text-transform:uppercase; letter-spacing:.8px; margin-bottom:8px; }
.kpi-value { font-size:25px; font-weight:700; color:#171717; line-height:1.2; }
.demo { background:#FFF4C7; border:1px solid #E7D487; color:#66540A; border-radius:9px; padding:11px 14px; margin-bottom:22px; font-size:13px; }
.info-box { background:#EAF3FF; border:1px solid #BFD8F5; color:#24496E; border-radius:9px; padding:13px 15px; margin:12px 0 22px; font-size:14px; }
.section { margin-top:26px; margin-bottom:10px; font-size:18px; font-weight:600; color:#1C1C1C; }
.footer { color:#8A8A8A; font-size:12px; text-align:center; margin-top:45px; padding-top:15px; border-top:1px solid #DED8CD; }
/* CONTRASTE MAHUT — indicadores nativos do Streamlit em fundo claro */
[data-testid="stMetric"] {
    background-color: #FFFFFF !important;
    border: 1px solid #E4DED3 !important;
    border-radius: 12px !important;
    padding: 14px 16px !important;
}
[data-testid="stMetricLabel"],
[data-testid="stMetricLabel"] *,
[data-testid="stMetricValue"],
[data-testid="stMetricValue"] *,
[data-testid="stMetricDelta"],
[data-testid="stMetricDelta"] * {
    opacity: 1 !important;
    -webkit-text-fill-color: #171717 !important;
    color: #171717 !important;
}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] * {
    color: #555047 !important;
    -webkit-text-fill-color: #555047 !important;
}
[data-testid="stMetricDelta"], [data-testid="stMetricDelta"] * {
    color: #80651F !important;
    -webkit-text-fill-color: #80651F !important;
}
/* MAHUT — Botões de download legíveis em todos os estados */
.stApp [data-testid="stDownloadButton"] button {
    background-color: #171717 !important;
    border: 1px solid #171717 !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    opacity: 1 !important;
}
.stApp [data-testid="stDownloadButton"] button *,
.stApp [data-testid="stDownloadButton"] button p {
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    opacity: 1 !important;
}
.stApp [data-testid="stDownloadButton"] button:hover,
.stApp [data-testid="stDownloadButton"] button:focus-visible {
    background-color: #80651F !important;
    border-color: #80651F !important;
    color: #FFFFFF !important;
}
.stApp [data-testid="stDownloadButton"] button:disabled {
    background-color: #77736D !important;
    border-color: #77736D !important;
}
/* MAHUT — contraste consistente de TODOS os botões de ação */
.stApp [data-testid="stButton"] button,
.stApp [data-testid="stFormSubmitButton"] button {
    background-color: #171717 !important;
    border: 1px solid #171717 !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    opacity: 1 !important;
}
.stApp [data-testid="stButton"] button *,
.stApp [data-testid="stFormSubmitButton"] button * {
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    opacity: 1 !important;
}
.stApp [data-testid="stButton"] button:hover,
.stApp [data-testid="stButton"] button:focus-visible,
.stApp [data-testid="stFormSubmitButton"] button:hover,
.stApp [data-testid="stFormSubmitButton"] button:focus-visible {
    background-color: #80651F !important;
    border-color: #80651F !important;
}
.stApp [data-testid="stButton"] button:disabled,
.stApp [data-testid="stFormSubmitButton"] button:disabled {
    background-color: #77736D !important;
    border-color: #77736D !important;
    cursor: not-allowed !important;
}

/* MAHUT — contraste dos avisos e campos em todos os estados */
[data-testid="stAlert"] { color:#302819 !important; }
[data-testid="stAlert"] p, [data-testid="stAlert"] span,
[data-testid="stAlert"] div { color:#302819 !important; -webkit-text-fill-color:#302819 !important; }
[data-testid="stWidgetLabel"] p,
[data-testid="stFileUploader"] label p,
[data-testid="stSelectbox"] label p,
[data-testid="stDateInput"] label p,
[data-testid="stTextInput"] label p {
 color:#34302B !important; -webkit-text-fill-color:#34302B !important;
 opacity:1 !important;
}
[data-testid="stFileUploaderDropzone"] {background:#FFF !important; border:1px dashed #A99C83 !important;}
[data-testid="stFileUploaderDropzone"] * {color:#242424 !important; -webkit-text-fill-color:#242424 !important;}
/* MAHUT — upload CSV: botão, texto e ícones com contraste independente do hover */
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] {
    background-color: #FFFFFF !important;
    border: 1px dashed #A99C83 !important;
}
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button,
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button:hover,
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button:focus-visible {
    background-color: #171717 !important;
    border: 1px solid #171717 !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    opacity: 1 !important;
}
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button *,
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button p,
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button span {
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    opacity: 1 !important;
}
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] button svg {
    color: #FFFFFF !important;
    stroke: #FFFFFF !important;
}
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] > div,
.stApp [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] small {
    color: #242424 !important;
    -webkit-text-fill-color: #242424 !important;
}
</style>
""", unsafe_allow_html=True)


def aviso_demo():
    st.markdown('<div class="demo"><b>AMBIENTE DE PROTÓTIPO:</b> os indicadores fora do módulo Vendas ainda podem ser demonstrativos. O módulo Vendas consulta pedidos reais da Nuvemshop e os dados GA4 são reais.</div>', unsafe_allow_html=True)


def rodape():
    st.markdown('<div class="footer">MAHUT · a essência inspirada por Deus · Protótipo BI</div>', unsafe_allow_html=True)


def moeda(valor):
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def numero_br(valor, casas=2):
    """Formata número para exibição brasileira, sem modificar dados de cálculo."""
    if valor is None or pd.isna(valor):
        return "—"
    return f"{float(valor):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def inteiro_br(valor):
    if valor is None or pd.isna(valor):
        return "—"
    return f"{int(valor):,}".replace(",", ".")


def tabela_br(dados, **kwargs):
    """Apresentação pt-BR em todas as tabelas do BI; não altera as bases numéricas.

    Colunas com R$ / capital / investimento recebem moeda; porcentagens recebem
    vírgula decimal; quantidades e saldos recebem milhar com ponto. Dataframes
    usados em cálculos e exportações permanecem intocados.
    """
    if not isinstance(dados, pd.DataFrame):
        dados = pd.DataFrame(dados)
    visual = dados.copy()
    for coluna in visual.columns:
        serie = visual[coluna]
        nome = str(coluna).casefold()
        if pd.api.types.is_datetime64_any_dtype(serie):
            visual[coluna] = pd.to_datetime(serie, errors="coerce").dt.strftime("%d/%m/%Y").fillna("—")
            continue
        if not pd.api.types.is_numeric_dtype(serie):
            # Colunas Decimal (objeto) também precisam de moeda brasileira.
            valores = serie.dropna()
            if len(valores) and all(isinstance(v, Decimal) for v in valores):
                if "%" in nome or "percentual" in nome or "margem" in nome:
                    visual[coluna] = serie.map(lambda v: numero_br(v, 2) + "%" if v is not None else "—")
                elif "r$" in nome or "capital" in nome or "investimento" in nome or "custo" in nome:
                    visual[coluna] = serie.map(lambda v: moeda(v) if v is not None else "—")
                elif all(v == v.to_integral_value() for v in valores):
                    visual[coluna] = serie.map(lambda v: inteiro_br(v) if v is not None else "—")
                else:
                    visual[coluna] = serie.map(lambda v: numero_br(v, 2) if v is not None else "—")
            elif nome == "data" and len(serie):
                visual[coluna] = serie.map(lambda v: v.strftime("%d/%m/%Y") if hasattr(v, "strftime") else v)
            continue
        if "%" in nome or "percentual" in nome or "margem" in nome:
            visual[coluna] = serie.map(lambda v: numero_br(v, 2) + "%" if pd.notna(v) else "—")
        elif "r$" in nome or "capital" in nome or "investimento" in nome or "custo unitário" in nome:
            visual[coluna] = serie.map(lambda v: moeda(v) if pd.notna(v) else "—")
        elif pd.api.types.is_integer_dtype(serie) or (len(serie.dropna()) and (serie.dropna() % 1 == 0).all()):
            visual[coluna] = serie.map(lambda v: inteiro_br(v) if pd.notna(v) else "—")
        else:
            visual[coluna] = serie.map(lambda v: numero_br(v, 2) if pd.notna(v) else "—")
    # Configurações numéricas em inglês sobrescreveriam a formatação brasileira.
    # Mantemos outras opções de apresentação, mas não as máscaras americanas.
    if "column_config" in kwargs:
        kwargs.pop("column_config")
    return st.dataframe(visual, **kwargs)


def classificar_abc_maHUT(registros):
    """ABC de Pareto por valor positivo; SKU na fronteira fica na classe iniciada."""
    positivos = [(chave, Decimal(str(valor))) for chave, valor in registros
                 if valor is not None and Decimal(str(valor)) > 0]
    positivos.sort(key=lambda x: (-x[1], str(x[0]).casefold()))
    total = sum((valor for _, valor in positivos), Decimal("0"))
    classes, participacoes, acumulados = {}, {}, {}
    acumulado = Decimal("0")
    for chave, valor in positivos:
        anterior = acumulado / total * 100
        classe = "A" if anterior < 80 else "B" if anterior < 95 else "C"
        acumulado += valor
        classes[chave] = classe
        participacoes[chave] = valor / total * 100
        acumulados[chave] = acumulado / total * 100
    return classes, participacoes, acumulados, total




# =========================================================
# ETAPA 8 — ALERTAS GERENCIAIS DE ESTOQUE (SOMENTE LEITURA)
# =========================================================
def gerar_alertas_gerenciais_maHUT(registros, linhas_cruz, linhas_giro,
                                   dias_operacao, dias_minimos, janela_giro):
    """Gera alertas auditáveis, sem alterar dados nem recomendar compra automaticamente."""
    from collections import Counter
    contagem_sku = Counter(
        str(r.get("SKU") or "").strip().casefold() for r in registros
        if str(r.get("SKU") or "").strip().casefold() not in ("", "não informado")
    )
    # O cruzamento preserva a ordem das variações do catálogo.
    # Não usar apenas SKU como chave: SKUs duplicados exigem conferência.
    giro_por_chave = {(r["Produto"], r["SKU"]): r for r in linhas_giro}
    cruz_por_chave = {(r["Produto"], r["SKU"]): r for r in linhas_cruz}
    positivos = [r for r in registros if r.get("Capital") is not None and r["Capital"] > 0]
    maiores = sorted(positivos, key=lambda r: r["Capital"], reverse=True)[:10]
    ids_maiores = {str(r.get("ID variação") or "") for r in maiores}
    historico_apto = dias_operacao >= dias_minimos and janela_giro >= dias_minimos
    alertas = []

    def incluir(reg, categoria, nivel, descricao, orientacao, natureza):
        alertas.append({
            "Nível": nivel, "Tipo de alerta": categoria,
            "Produto": reg["Produto"], "SKU": reg["SKU"],
            "Saldo (un.)": reg.get("Saldo"),
            "Capital (R$)": reg.get("Capital"),
            "Detalhe": descricao, "Próxima verificação": orientacao,
            "Natureza": natureza,
        })

    for reg in registros:
        sku = str(reg.get("SKU") or "").strip()
        chave_sku = sku.casefold()
        cruz = cruz_por_chave.get((reg["Produto"], reg["SKU"]), {})
        giro = giro_por_chave.get((reg["Produto"], reg["SKU"]), {})
        saldo = reg.get("Saldo")
        custo = reg.get("Custo unitário")
        if saldo == 0:
            incluir(reg, "Estoque zerado", "Crítico", "Saldo controlado igual a zero.",
                    "Conferir disponibilidade e necessidade de reposição.", "Objetivo")
        if saldo is None:
            incluir(reg, "Cadastro / saldo", "Atenção", "Saldo ausente ou sem controle de estoque.",
                    "Conferir gestão de estoque e saldo na Nuvemshop/Bling.", "Objetivo")
        if custo is None:
            incluir(reg, "Cadastro / custo", "Atenção", "Custo unitário não informado ou inválido.",
                    "Conferir custo cadastrado na Nuvemshop.", "Objetivo")
        if not chave_sku or chave_sku == "não informado":
            incluir(reg, "Cadastro / SKU", "Atenção", "SKU não informado.",
                    "Cadastrar SKU único e conferir integração com Bling.", "Objetivo")
        elif contagem_sku[chave_sku] > 1:
            incluir(reg, "Cadastro / SKU", "Atenção", "SKU repetido no catálogo.",
                    "Verificar duplicidade e vínculos entre variações.", "Objetivo")
        if str(reg.get("ID variação") or "") in ids_maiores:
            incluir(reg, "Capital elevado", "Informativo", "Entre os 10 maiores capitais por variação.",
                    "Acompanhar concentração de investimento e vendas por SKU.", "Objetivo")
        if cruz.get("ABC estoque") == "A" and cruz.get("ABC vendas") in ("B", "C", "Sem venda identificada"):
            incluir(reg, "Alto capital / poucas vendas", "Observação",
                    "Classe A de capital e vendas online B/C ou não identificadas no período.",
                    "Acompanhar campanhas e demanda de outros canais; não reduzir automaticamente.",
                    "Preliminar")
        if (historico_apto and saldo is not None and saldo > 0
                and giro.get("Cobertura") is not None
                and giro.get("Unidades vendidas", Decimal("0")) >= 5
                and cruz.get("Pedidos com SKU") is not None
                and cruz.get("Pedidos com SKU") >= 3
                and giro["Cobertura"] < 7):
            incluir(reg, "Cobertura baixa", "Atenção",
                    f"Cobertura online estimada: {numero_br(giro['Cobertura'], 1)} dias.",
                    "Verificar vendas dos outros canais e prazo de reposição antes de comprar.",
                    "Preliminar")
    prioridade = {"Crítico": 0, "Atenção": 1, "Observação": 2, "Informativo": 3}
    return sorted(alertas, key=lambda a: (prioridade[a["Nível"]], a["Tipo de alerta"],
                                         -(a["Capital (R$)"] or Decimal("0")), a["Produto"])), historico_apto

def taxa_pagamento_estimada(pedido):
    """Estimativa por modalidade; não representa tarifa contratada ou liquidada."""
    detalhes = pedido.get("payment_details") or {}
    if not isinstance(detalhes, dict):
        detalhes = {}
    metodo = str(detalhes.get("method") or "").strip().lower()
    total = valor_monetario(pedido.get("total"))
    if total is None or total < 0:
        return None, "Indisponível", "Não identificado"
    if metodo == "pix":
        taxa = total * Decimal("0.0099")
        nome = "Pix"
    elif metodo in ("credit_card", "credit-card", "credit card"):
        parcelas = valor_monetario(detalhes.get("installments"))
        if parcelas != Decimal("1"):
           return None, "Indisponível — parcelamento não validado", "Cartão de crédito"
        taxa = total * Decimal("0.0567") + Decimal("0.36")
        nome = "Cartão de crédito à vista"
    else:
        return None, "Indisponível — modalidade sem regra", metodo or "Não identificado"
    return taxa.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "Estimada", nome


def taxas_pedidos(pedidos, numeros):
    """Produz taxas estimadas dos pedidos pagos selecionados."""
    registros = {}
    for pedido in pedidos:
        numero = str(pedido.get("number") or pedido.get("id") or "—")
        if numero not in numeros:
           continue
        if str(pedido.get("payment_status") or "").lower() != "paid":
           continue
        taxa, origem, metodo = taxa_pagamento_estimada(pedido)
        registros[numero] = {"taxa": taxa, "origem": origem, "metodo": metodo}
    return registros


# Valores conferidos nos comprovantes do Nuvem Pago compartilhados pela MAHUT.
# Não são obtidos automaticamente da API; a origem deve permanecer explícita.
TAXAS_REAIS_CONFERIDAS = {
    "110": Decimal("0.55"),
    "109": Decimal("0.65"),
    "103": Decimal("0.41"),
    "108": Decimal("1.25"),
    "104": Decimal("1.51"),
    "102": Decimal("2.50"),
    "100": Decimal("3.29"),
}


def decimal_br(valor):
    """Lê valores monetários com vírgula decimal, sem permitir negativos."""
    texto = str(valor or "").strip().replace("R$", "").replace(" ", "")
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    numero = valor_monetario(texto)
    if numero is None or numero < 0:
        return None
    return numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def valor_br_sem_simbolo(valor):
    return f"{valor:.2f}".replace(".", ",") if valor is not None else ""



def filtrar_pedidos_dre(pedidos, inicio, fim):
    selecionados = []
    for pedido in pedidos:
        dia = data_pedido(pedido.get("created_at"))
        if dia is None:
            continue
        if inicio <= dia <= fim:
            selecionados.append(pedido)
    return selecionados


def compor_dre(pedidos, catalogo, taxas_reais):
    """DRE gerencial provisória, em regime de pedidos pagos por data de criação."""
    pagos = [
        p for p in pedidos
        if str(p.get("payment_status") or "").lower() == "paid"
        and str(p.get("status") or "").lower() not in ("cancelled", "canceled", "cancelado")
    ]
    cpvs, pendencias = cpv_pedidos(pagos, catalogo)
    mapa_cpv = {str(r["Pedido"]): r for r in cpvs}
    detalhes = []
    for pedido in pagos:
        numero = str(pedido.get("number") or pedido.get("id") or "—")
        total = valor_monetario(pedido.get("total"))
        desconto = valor_monetario(pedido.get("discount"))
        frete_recebido = valor_monetario(pedido.get("shipping_cost_customer"))
        custo_envio = valor_monetario(pedido.get("shipping_cost_owner"))
        taxa_estimativa, origem, metodo = taxa_pagamento_estimada(pedido)
        taxa_real = decimal_br(taxas_reais.get(numero, ""))
        taxa = taxa_real if taxa_real is not None else taxa_estimativa
        origem = "Real — comprovante" if taxa_real is not None else origem
        cpv_registro = mapa_cpv.get(numero)
        cpv = cpv_registro["CPV"] if cpv_registro and cpv_registro["Completo"] else None
        detalhes.append({
            "Pedido": numero, "Data": data_pedido(pedido.get("created_at")),
            "Pagamento": metodo, "Origem da taxa": origem,
            "Total": total, "Desconto": desconto,
            "Frete recebido": frete_recebido, "Custo de envio API": custo_envio,
            "CPV": cpv, "Taxa": taxa,
        })
    return detalhes, pendencias


def somar_dre(registros, chave):
    valores = [r[chave] for r in registros]
    if any(v is None for v in valores):
        return None
    return sum(valores, Decimal("0"))


def subtrair_dre(*valores):
    if any(v is None for v in valores):
        return None
    return valores[0] - sum(valores[1:], Decimal("0"))


def percentual_dre(valor, base):
    if valor is None or base is None or base == 0:
        return "Indisponível"
    return f"{valor / base * 100:.2f}%".replace(".", ",")


def exibir_linha_dre(nome, valor, base, observacao=""):
    return {
        "Descrição": nome,
        "Valor (R$)": moeda(valor) if valor is not None else "Pendente",
        "% da receita": percentual_dre(valor, base),
        "Observação": observacao,
    }


def card(titulo, valor):
    st.markdown(f'<div class="card"><div class="kpi-label">{titulo}</div><div class="kpi-value">{valor}</div></div>', unsafe_allow_html=True)


# DADOS DEMONSTRATIVOS — NÃO REPRESENTAM RESULTADOS REAIS

datas = pd.date_range(start=datetime.today() - timedelta(days=29), periods=30, freq="D")
valores = [420, 510, 390, 650, 720, 480, 560, 610, 530, 690,
           740, 620, 580, 760, 810, 590, 630, 710, 680, 790,
           840, 720, 660, 880, 910, 750, 820, 940, 870, 960]
df = pd.DataFrame({"Data": datas, "Faturamento": valores})
faturamento_demo = 17500.00
pedidos_demo = 173
ticket_demo = faturamento_demo / pedidos_demo
meta_demo = 10000.00
produtos = pd.DataFrame({
    "Produto": ["HADAN 60 ML", "IMPERIUM 60 ML", "KHALID 60 ML", "LILIUM 60 ML", "MALIK 60 ML"],
    "Faturamento": [4200, 3600, 3300, 3100, 2800],
    "Unidades": [34, 28, 27, 25, 22],
})
estoque = pd.DataFrame({
    "Produto": ["HADAN 60 ML", "LILIUM 60 ML", "KHALID 60 ML", "MALIK 60 ML", "IMPERIUM 60 ML"],
    "Saldo": [18, 8, 14, 11, 16],
    "Giro": ["Médio", "Alto", "Médio", "Médio", "Baixo"],
    "Cobertura": ["Adequada", "Atenção", "Adequada", "Adequada", "Alta"],
})

with st.sidebar:
    st.markdown('<div style="font-size:28px;font-weight:700;letter-spacing:5px">MAHUT</div>', unsafe_allow_html=True)
    st.caption("BUSINESS INTELLIGENCE")
    pagina = st.radio("Navegação", ["Home", "1 · Visão Geral", "2 · Vendas", "3 · Produtos",
                                      "4 · Estoque", "5 · Financeiro", "6 · Marketing",
                                      "7 · Influenciadores", "8 · Metas & Indicadores", "9 · DRE Gerencial", "Configurações"],
                      label_visibility="collapsed", key="mahut_navegacao_principal")
    st.markdown("---")
    st.caption("Fontes planejadas")
    st.write("Nuvemshop · Bling · GA4 · Meta")

st.markdown('<div class="mahut-title">MAHUT</div>', unsafe_allow_html=True)
st.markdown(f'<div class="mahut-sub">BI Executivo · {pagina}</div>', unsafe_allow_html=True)

if pagina in ("Home", "1 · Visão Geral"):
    aviso_demo()
    st.markdown("## Painel Executivo" if pagina == "Home" else "## Visão Geral")
    c1, c2, c3, c4 = st.columns(4)
    with c1: card("Faturamento — 30 dias", moeda(faturamento_demo))
    with c2: card("Pedidos", str(pedidos_demo))
    with c3: card("Ticket médio", moeda(ticket_demo))
    with c4: card("Meta mensal", moeda(meta_demo))
    st.markdown('<div class="section">Evolução do faturamento</div>', unsafe_allow_html=True)
    st.line_chart(df.set_index("Data")[["Faturamento"]], use_container_width=True)

elif pagina == "2 · Vendas":
    st.markdown("## Vendas — dados reais da Nuvemshop")
    st.caption("Valores retornados pela Nuvemshop. Consulta armazenada por até 5 minutos.")
    if st.button("Atualizar pedidos agora"):
        consultar_pedidos_nuvemshop.clear()
        st.rerun()
    try:
        pedidos_reais = consultar_pedidos_nuvemshop()
        tabela_todos = consolidar_vendas(pedidos_reais)
        st.markdown("### Período de análise")
        periodo = st.selectbox("Selecionar período", [
            "Todos os pedidos", "Hoje", "Últimos 7 dias", "Últimos 30 dias",
            "Este mês", "Período personalizado",
        ])
        hoje = datetime.now().date()
        inicio = fim = None
        if periodo == "Hoje":
            inicio = fim = hoje
        elif periodo == "Últimos 7 dias":
            inicio, fim = hoje - timedelta(days=6), hoje
        elif periodo == "Últimos 30 dias":
            inicio, fim = hoje - timedelta(days=29), hoje
        elif periodo == "Este mês":
            inicio, fim = hoje.replace(day=1), hoje
        elif periodo == "Período personalizado":
            datas_escolhidas = st.date_input(
                "De / até", value=(hoje - timedelta(days=29), hoje),
                format="DD/MM/YYYY",
            )
            if isinstance(datas_escolhidas, (tuple, list)) and len(datas_escolhidas) == 2:
                inicio, fim = datas_escolhidas
            else:
                st.info("Selecione a data inicial e a data final para aplicar o filtro.")
                inicio, fim = hoje, hoje - timedelta(days=1)
        tabela = tabela_todos.copy()
        if inicio is not None:
            tabela = tabela[tabela["Data"].notna() &
                            (tabela["Data"] >= inicio) & (tabela["Data"] <= fim)].copy()
        contagens, confirmado, pendente, frete, sem_frete, sem_total = indicadores_vendas(tabela)
        qtd_pagos = contagens["Pago"]
        ticket = confirmado / qtd_pagos if qtd_pagos else Decimal("0")
        c1, c2, c3, c4 = st.columns(4)
        with c1: card("Faturamento confirmado", moeda(confirmado))
        with c2: card("Pedidos pagos", str(qtd_pagos))
        with c3: card("Ticket médio dos pagos", moeda(ticket))
        with c4: card("Valor pendente", moeda(pendente))
        st.markdown("### Situação dos pedidos")
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.metric("Pendentes", contagens["Pendente"])
        with c2: st.metric("Cancelados", contagens["Cancelado"])
        with c3: st.metric("Reembolsados / parciais", contagens["Reembolsado / parcial"])
        with c4: st.metric("Outros status", contagens["Outro status"])
        st.markdown("### Produtos e frete — pedidos pagos")
        c1, c2 = st.columns(2)
        with c1: st.metric("Frete cobrado identificado", moeda(frete))
        with c2:
            if sem_frete == 0:
                st.metric("Total menos frete cobrado", moeda(confirmado - frete))
            else:
                st.metric("Total menos frete cobrado", "Indisponível")
        if sem_frete:
            st.warning(f"Há {sem_frete} pedido(s) pago(s) sem frete informado. Não é possível apurar com segurança o valor sem frete.")
        if sem_total:
            st.warning(f"Há {sem_total} pedido(s) sem valor total válido. Confira a Nuvemshop.")
        st.caption("Total menos frete é uma aproximação do valor vendido sem o frete cobrado do cliente; não é receita líquida e não desconta taxas, impostos nem eventuais reembolsos.")
        st.markdown("### Evolução diária das vendas pagas")
        pagos = tabela[(tabela["Pagamento"] == "Pago") & tabela["Data"].notna()].copy()
        if pagos.empty:
            st.info("Não há pedidos pagos com data válida no período selecionado.")
        else:
            evolucao = (pagos.groupby("Data", as_index=False)["Valor do pedido (R$)"]
                       .sum().rename(columns={"Valor do pedido (R$)": "Vendas pagas (R$)"}))
            # Completar dias sem vendas, respeitando o período escolhido.
            primeiro_dia = inicio if inicio is not None else evolucao["Data"].min()
            ultimo_dia = fim if fim is not None else evolucao["Data"].max()
            dias = pd.date_range(start=primeiro_dia, end=ultimo_dia, freq="D")
            evolucao["Data"] = pd.to_datetime(evolucao["Data"])
            evolucao = (evolucao.set_index("Data")
                        .reindex(dias, fill_value=0)
                        .rename_axis("Data").reset_index())
            evolucao["Data brasileira"] = evolucao["Data"].dt.strftime("%d/%m")
            # Eixo categórico: impede que o navegador exiba horários ou nomes em inglês.
            grafico = (alt.Chart(evolucao).mark_line(point=True)
                       .encode(
                           x=alt.X("Data brasileira:N", sort=evolucao["Data brasileira"].tolist(),
                                   title="Data (dia/mês)", axis=alt.Axis(labelAngle=-45)),
                           y=alt.Y("Vendas pagas (R$):Q", title="Vendas pagas (R$)",
                                   scale=alt.Scale(zero=True)),
                           tooltip=[alt.Tooltip("Data brasileira:N", title="Data"),
                                    alt.Tooltip("Vendas pagas (R$):Q", title="Vendas pagas", format=".2f")]
                       ).properties(height=320))
            st.altair_chart(grafico, use_container_width=True)
        st.markdown("### Detalhamento dos pedidos")
        colunas = ["Pedido", "Data", "Pagamento", "Valor do pedido (R$)",
                   "Frete cobrado (R$)", "Subtotal (R$)", "Desconto informado (R$)"]
        exibicao = tabela[colunas].sort_values("Data", ascending=False, na_position="last")
        if exibicao.empty:
            st.info("Nenhum pedido encontrado para o período selecionado.")
        else:
            exibicao_br = exibicao.copy()
            for nome in colunas:
                if nome.endswith("(R$)"):
                    exibicao_br[nome] = exibicao_br[nome].map(lambda v: moeda(v) if pd.notna(v) else "Pendente")
            tabela_br(exibicao_br, use_container_width=True, hide_index=True,
                         column_config={"Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY")})
        st.caption("Os filtros usam a data de criação do pedido, não a data do pagamento. Os valores são brutos conforme a API, e pedidos cancelados não entram no faturamento confirmado.")
        st.caption("Pedidos reembolsados ou parcialmente reembolsados ficam separados até a conciliação financeira. Datas seguem a data civil informada pela API.")
    except Exception as erro:
        st.error("Não foi possível carregar os dados de vendas da Nuvemshop. Confira a conexão em Configurações.")
        st.caption(f"Detalhe técnico: {type(erro).__name__}")

elif pagina == "3 · Produtos":
    st.markdown("## Produtos — rentabilidade real da Nuvemshop")
    st.caption("Pedidos pagos; receita de produtos sem frete cobrado. Descontos do pedido são refletidos no valor líquido das mercadorias, rateado proporcionalmente entre os itens.")
    st.warning("Análise gerencial provisória: CPV usa custos atuais do catálogo; imposto de 4% é estimado; taxas podem ser estimadas. Frete de envio, publicidade, comissões e despesas fixas NÃO são abatidos na margem por produto. Não é lucro líquido.")
    if st.button("Atualizar dados de produtos agora", key="atualizar_produtos_reais"):
        consultar_pedidos_nuvemshop.clear()
        consultar_produtos_nuvemshop_diagnostico.clear()
        st.rerun()
    try:
        pedidos_prod = consultar_pedidos_nuvemshop()
        catalogo_prod = consultar_produtos_nuvemshop_diagnostico()
        hoje_prod = datetime.now().date()
        periodo_prod = st.selectbox("Período da rentabilidade", ["Este mês", "Últimos 30 dias", "Este ano", "Todos os pedidos", "Período personalizado"], key="periodo_produtos")
        if periodo_prod == "Este mês":
            inicio_prod, fim_prod = hoje_prod.replace(day=1), hoje_prod
        elif periodo_prod == "Últimos 30 dias":
            inicio_prod, fim_prod = hoje_prod - timedelta(days=29), hoje_prod
        elif periodo_prod == "Este ano":
            inicio_prod, fim_prod = hoje_prod.replace(month=1, day=1), hoje_prod
        elif periodo_prod == "Todos os pedidos":
            inicio_prod, fim_prod = datetime(2000, 1, 1).date(), hoje_prod
        else:
            intervalo_prod = st.date_input("Data inicial / final", value=(hoje_prod - timedelta(days=29), hoje_prod), format="DD/MM/YYYY", key="datas_produtos")
            if not isinstance(intervalo_prod, (tuple, list)) or len(intervalo_prod) != 2:
                st.info("Selecione as duas datas para consultar o período.")
                st.stop()
            inicio_prod, fim_prod = intervalo_prod
        pedidos_prod = filtrar_pedidos_dre(pedidos_prod, inicio_prod, fim_prod)
        taxas_prod = st.session_state.get("taxas_reais_maHUT", {
            numero: valor_br_sem_simbolo(valor) for numero, valor in TAXAS_REAIS_CONFERIDAS.items()})
        linhas_prod, alertas_prod = linhas_rentabilidade_produtos(pedidos_prod, catalogo_prod, taxas_prod)
        consolidados_prod = tabela_rentabilidade_produtos(linhas_prod)
        if not linhas_prod:
            st.info("Não há itens de pedidos pagos com dados suficientes no período.")
        else:
            st.markdown("### Resultado consolidado por perfume")
            completos_prod = all(r["Contribuição"] is not None for r in linhas_prod)
            receita_prod = sum((r["Receita"] for r in linhas_prod), Decimal("0"))
            contrib_prod = sum((r["Contribuição"] for r in linhas_prod if r["Contribuição"] is not None), Decimal("0"))
            a, b, c, d = st.columns(4)
            with a: st.metric("Receita dos produtos", moeda(receita_prod))
            with b: st.metric("Unidades", f"{sum(r['Unidades'] for r in linhas_prod):g}")
            with c: st.metric("Contribuição preliminar", moeda(contrib_prod) if completos_prod else "Incompleta")
            with d: st.metric("Margem de contribuição", f"{contrib_prod / receita_prod * 100:.2f}%".replace(".", ",") if completos_prod and receita_prod else "Incompleta")
            # Indicadores e rankings usam todos os itens do período, sem excluir o pedido #109.
            st.markdown("### Indicadores consolidados de rentabilidade")
            cpv_completo_prod = all(r["CPV"] is not None for r in linhas_prod)
            cpv_total_prod = sum((r["CPV"] for r in linhas_prod if r["CPV"] is not None), Decimal("0"))
            i1, i2, i3, i4 = st.columns(4)
            with i1: st.metric("Receita de mercadorias", moeda(receita_prod))
            with i2: st.metric("CPV total", moeda(cpv_total_prod) if cpv_completo_prod else "Incompleto")
            with i3: st.metric("Contribuição total", moeda(contrib_prod) if completos_prod else "Incompleta")
            with i4:
                margem_ponderada = (contrib_prod / receita_prod * Decimal("100")
                                    if completos_prod and receita_prod > 0 else None)
                st.metric("Margem ponderada", f"{margem_ponderada:.2f}%".replace(".", ",")
                          if margem_ponderada is not None else "Incompleta")
            st.caption("A margem ponderada é contribuição total ÷ receita total; não é a média simples das margens dos perfumes.")

            exibir_tabela_rentabilidade(consolidados_prod)

            st.markdown("### Ranking de rentabilidade dos perfumes")
            ranking_validos = [r for r in consolidados_prod if r["Contribuição"] is not None]
            ranking_pendentes = len(consolidados_prod) - len(ranking_validos)
            if ranking_pendentes:
                st.warning(f"{ranking_pendentes} produto(s) com dados incompletos ficaram fora dos rankings; consulte as pendências de conciliação.")
            if ranking_validos:
                criterio_ranking = st.selectbox(
                    "Ordenar ranking por", ["Maior contribuição (R$)", "Maior margem (%)", "Maior receita (R$)"],
                    key="criterio_ranking_rentabilidade")
                chave_ranking = {"Maior contribuição (R$)": "Contribuição",
                                 "Maior margem (%)": "Margem", "Maior receita (R$)": "Receita"}[criterio_ranking]
                ranking_ordenado = sorted(
                    [r for r in ranking_validos if r.get(chave_ranking) is not None],
                    key=lambda r: (r[chave_ranking], r["Receita"]), reverse=True)
                ranking_tabela = []
                for posicao, r in enumerate(ranking_ordenado, start=1):
                    ranking_tabela.append({
                        "Posição": posicao, "Produto": r["Produto"], "SKU": r["SKU"],
                        "Unidades": float(r["Unidades"]), "Pedidos": r["Pedidos"],
                        "Receita (R$)": moeda(r["Receita"]),
                        "CPV (R$)": moeda(r["CPV"]),
                        "Contribuição (R$)": moeda(r["Contribuição"]),
                        "Margem (%)": f"{r['Margem']:.2f}%".replace(".", ",") if r["Margem"] is not None else "Pendente",
                    })
                tabela_br(pd.DataFrame(ranking_tabela), use_container_width=True, hide_index=True)
                st.caption("O ranking considera somente produtos com CPV e taxas conciliados. Não desconta frete de envio, publicidade nem despesas fixas.")
            else:
                st.info("Ainda não há produtos com custos e taxas completos para o ranking.")

            st.markdown("### Venda a venda")
            nomes_prod = sorted({r["Produto"] for r in linhas_prod})
            escolha_prod = st.selectbox("Filtrar perfume", ["Todos os perfumes"] + nomes_prod, key="filtro_perfume_venda")
            detalhamento_prod = [r for r in linhas_prod if escolha_prod == "Todos os perfumes" or r["Produto"] == escolha_prod]
            exibir_tabela_rentabilidade(sorted(detalhamento_prod, key=lambda r: (r["Data"] or datetime(2000, 1, 1).date(), r["Pedido"]), reverse=True), incluir_pedido=True)
            st.caption("Quando há mais de um produto no pedido, o valor pago pelas mercadorias e a taxa de pagamento são distribuídos conforme o valor dos itens antes do desconto. Impostos são estimados em 4% da receita atribuída. O frete fica fora da margem individual.")
        if alertas_prod:
            with st.expander(f"Pendências de conciliação ({len(alertas_prod)})"):
                for aviso in alertas_prod:
                    st.write("• " + aviso)
    except Exception as erro:
        st.error("Não foi possível calcular a rentabilidade por produto. Confira as integrações em Configurações.")
        st.caption(f"Detalhe técnico: {type(erro).__name__}: {erro}")

elif pagina == "4 · Estoque":
    st.markdown("## Estoque — loja virtual MAHUT")
    st.caption("Fonte: saldos e custos atuais das variações na Nuvemshop, sincronizada com o Bling. Consulta em modo somente leitura; cache de até 5 minutos.")
    st.info("O saldo e o capital vêm do catálogo atual. Giro e cobertura são indicadores preliminares baseados somente nos pedidos pagos da loja virtual; a divulgação começou recentemente e outros canais podem movimentar o mesmo estoque. Não representam previsão confiável de demanda total nem recomendação automática de compra.")
    if st.button("Atualizar estoque agora", key="atualizar_estoque_real"):
        consultar_produtos_nuvemshop_diagnostico.clear()
        consultar_pedidos_nuvemshop.clear()
        st.rerun()
    try:
        catalogo_estoque = consultar_produtos_nuvemshop_diagnostico()
        registros_estoque = linhas_estoque_nuvemshop(catalogo_estoque)
        if not registros_estoque:
            st.warning("A API não retornou variações de produtos. Confira o catálogo na Nuvemshop.")
        else:
            total_variacoes = len(registros_estoque)
            validos_estoque = [r for r in registros_estoque if r["Saldo"] is not None]
            avaliados_estoque = [r for r in validos_estoque if r["Capital"] is not None]
            pendentes_estoque = total_variacoes - len(avaliados_estoque)
            saldo_total = sum(r["Saldo"] for r in validos_estoque)
            capital_total = sum((r["Capital"] for r in avaliados_estoque), Decimal("0"))
            zerados = sum(r["Saldo"] == 0 for r in validos_estoque)
            a, b, c, d = st.columns(4)
            with a: st.metric("Variações cadastradas", total_variacoes)
            with b: st.metric("Unidades com saldo conhecido", f"{saldo_total:,}".replace(",", "."))
            with c: st.metric("Variações zeradas", zerados)
            with d:
                st.metric("Capital em estoque", moeda(capital_total) if not pendentes_estoque else "Incompleto")
            if pendentes_estoque:
                st.warning(f"{pendentes_estoque} variação(ões) sem saldo controlado ou custo válido. O capital total não pode ser considerado completo. Capital identificado até agora: {moeda(capital_total)}.")
            st.markdown("### Saldo por perfume e SKU")
            busca_estoque = st.text_input("Buscar perfume ou SKU", key="busca_estoque")
            opcoes_situacao = ["Todas as situações", "Disponível", "Zerado", "Saldo indisponível", "Sem controle de estoque"]
            situacao_estoque = st.selectbox("Filtrar situação", opcoes_situacao, key="situacao_estoque")
            selecionados_estoque = [r for r in registros_estoque
                                  if (not busca_estoque or busca_estoque.casefold() in (r["Produto"] + " " + r["SKU"]).casefold())
                                  and (situacao_estoque == "Todas as situações" or r["Situação"] == situacao_estoque)]
            tabela_estoque = []
            for r in sorted(selecionados_estoque, key=lambda x: (x["Produto"].casefold(), x["SKU"].casefold())):
                tabela_estoque.append({
                    "Produto": r["Produto"], "SKU": r["SKU"],
                    "Saldo (un.)": r["Saldo"],
                    "Custo unitário (R$)": moeda(r["Custo unitário"]) if r["Custo unitário"] is not None else "Pendente",
                    "Capital (R$)": moeda(r["Capital"]) if r["Capital"] is not None else "Pendente",
                    "Situação": r["Situação"],
                })
            tabela_br(pd.DataFrame(tabela_estoque, columns=["Produto", "SKU", "Saldo (un.)", "Custo unitário (R$)", "Capital (R$)", "Situação"]),
                         use_container_width=True, hide_index=True)
            st.caption("Saldo zero só é indicado quando a API retorna explicitamente zero e o controle de estoque não está desativado. Produtos sem saldo válido aparecem como pendentes, nunca como zero.")

            st.markdown("### Giro e cobertura — leitura inicial")
            st.caption("Movimentação considerada: quantidade dos itens em pedidos pagos e não cancelados da Nuvemshop, pela data de criação. Não inclui saídas do Bling, vendas diretas ou outros canais; o pedido de teste #109 pode ser desconsiderado dos cálculos pelo filtro abaixo, sem excluir o registro.")
            janela_giro = st.selectbox("Janela de análise", [7, 15, 30, 60, 90], index=2,
                                      format_func=lambda n: f"Últimos {n} dias", key="janela_giro_estoque")
            fim_giro = datetime.now().date()
            inicio_giro = fim_giro - timedelta(days=janela_giro - 1)
            pedidos_estoque_originais = consultar_pedidos_nuvemshop()
            excluir_teste_109 = st.checkbox(
                "Desconsiderar pedido de teste #109 nas análises comerciais do estoque",
                value=True, key="excluir_teste_109_estoque_maHUT",
                help="Retira apenas o pedido #109 dos cálculos de giro, cobertura, ABC de vendas, alertas e previsão. Não apaga nem altera o pedido na Nuvemshop.")
            def pedido_teste_109_maHUT(pedido):
                return str(pedido.get("number") or "").strip() == "109"
            testes_encontrados = [p for p in pedidos_estoque_originais if pedido_teste_109_maHUT(p)]
            pedidos_giro = ([p for p in pedidos_estoque_originais if not pedido_teste_109_maHUT(p)]
                            if excluir_teste_109 else pedidos_estoque_originais)
            if excluir_teste_109:
                st.caption(f"Filtro de teste ativo: {inteiro_br(len(testes_encontrados))} pedido(s) #109 desconsiderado(s) "
                           "dos indicadores comerciais deste módulo. Saldos e custos reais permanecem inalterados.")
                if not testes_encontrados:
                    st.warning("O pedido #109 não foi encontrado na consulta atual. Confirme o número do pedido na Nuvemshop.")
            else:
                st.warning("O pedido de teste #109 está incluído nas análises comerciais. "
                           "As projeções podem ficar artificialmente elevadas.")
            linhas_giro, qtd_pedidos_giro, sem_chave_giro = analise_giro_estoque(
                registros_estoque, pedidos_giro, inicio_giro, fim_giro)
            unidades_giro = sum((r["Unidades vendidas"] for r in linhas_giro), Decimal("0"))
            itens_com_venda = sum(r["Unidades vendidas"] > 0 for r in linhas_giro)
            g1, g2, g3 = st.columns(3)
            with g1: st.metric("Pedidos pagos na janela", qtd_pedidos_giro)
            with g2: st.metric("Unidades atribuídas a SKUs", f"{unidades_giro:g}")
            with g3: st.metric("Variações com venda", itens_com_venda)
            if janela_giro < 30:
                st.warning("Janela curta: use somente para observar movimentação recente. Não há histórico suficiente para definir aumento ou redução de estoque.")
            else:
                st.warning("A divulgação da loja começou há poucos dias. Mesmo em janelas de 30 dias ou mais, a amostra pode ser insuficiente. Valide também as saídas de outros canais antes de comprar ou reduzir estoque.")
            if sem_chave_giro:
                st.warning(f"{sem_chave_giro} item(ns) de pedido sem SKU/ID da variação não puderam ser atribuídos a um perfume.")
            st.caption("Cobertura indicativa (dias) = saldo atual ÷ (unidades vendidas na janela ÷ dias da janela). Sem vendas, a cobertura é indeterminada — não infinita. A métrica NÃO prevê o consumo de outros canais.")
            tabela_giro = []
            for r in sorted(linhas_giro, key=lambda x: (x["Unidades vendidas"], x["Produto"]), reverse=True):
                tabela_giro.append({
                    "Produto": r["Produto"], "SKU": r["SKU"],
                    "Saldo atual": r["Saldo"],
                    "Vendido (un.)": float(r["Unidades vendidas"]),
                    "Média/dia": f"{r['Média diária']:.3f}".replace(".", ","),
                    "Cobertura indicativa": (f"{r['Cobertura']:.1f} dias".replace(".", ",")
                                             if r["Cobertura"] is not None else "Indeterminada"),
                    "Capital (R$)": moeda(r["Capital"]) if r["Capital"] is not None else "Pendente",
                    "Interpretação": r["Leitura"],
                })
            tabela_br(pd.DataFrame(tabela_giro), use_container_width=True, hide_index=True)
            st.caption("A ordenação mostra primeiro os SKUs com mais unidades vendidas. Nenhum item é automaticamente rotulado como excesso de estoque ou produto parado.")

            st.markdown("### Inteligência de reposição — apoio à decisão")
            st.info("Esta análise é orientativa, não é uma ordem de compra. Como a divulgação da loja é recente, ausência de vendas não prova baixa demanda. Considere também vendas fora da Nuvemshop, sazonalidade, campanhas e estoque compartilhado.")
            col_hist, col_min, col_alvo = st.columns(3)
            with col_hist:
                dias_operacao = st.number_input(
                    "Dias de divulgação ativa da loja", min_value=1, max_value=3650,
                    value=7, step=1, key="dias_divulgacao_maHUT",
                    help="Informe há quantos dias a loja vem recebendo divulgação comercial efetiva. Não confundir com a idade do cadastro da loja.")
            with col_min:
                dias_minimos = st.number_input(
                    "Histórico mínimo para análise", min_value=14, max_value=180,
                    value=30, step=1, key="dias_minimos_reposicao_maHUT")
            with col_alvo:
                cobertura_alvo = st.number_input(
                    "Cobertura de referência (dias)", min_value=7, max_value=120,
                    value=30, step=1, key="cobertura_alvo_maHUT")

            st.caption("A classificação usa as vendas pagas identificadas por SKU/variação na janela selecionada acima. Critério conservador: pelo menos 30 dias de divulgação ativa (ajustável), 3 pedidos pagos atribuídos ao SKU e 5 unidades vendidas para avaliar tendência. Esses limites são parâmetros gerenciais iniciais, não regras universais.")
            historico_suficiente = dias_operacao >= dias_minimos and janela_giro >= dias_minimos
            dados_decisao = []
            for r in linhas_giro:
                saldo = r["Saldo"]
                unidades = r["Unidades vendidas"]
                media = r["Média diária"]
                cobertura = r["Cobertura"]
                # Reconta pedidos distintos por SKU/ID, evitando tomar 6 unidades
                # de uma única compra como 6 sinais independentes de demanda.
                ids_variacao = {x["ID variação"] for x in registros_estoque
                                if x["Produto"] == r["Produto"] and x["SKU"] == r["SKU"]}
                pedidos_sku = set()
                for ped in pedidos_giro:
                    dia_ped = data_pedido(ped.get("created_at"))
                    if dia_ped is None or not inicio_giro <= dia_ped <= fim_giro:
                        continue
                    if str(ped.get("payment_status") or "").lower() != "paid":
                        continue
                    if str(ped.get("status") or "").lower() in ("cancelled", "canceled", "cancelado"):
                        continue
                    for item in ped.get("products") or []:
                        if not isinstance(item, dict):
                            continue
                        var_item = item.get("variant") if isinstance(item.get("variant"), dict) else {}
                        vid_item = str(item.get("variant_id") or var_item.get("id") or "").strip()
                        sku_item = str(item.get("sku") or "").strip().casefold()
                        # Repete a mesma precedência ID -> SKU do cálculo de giro.
                        corresponde = (vid_item in ids_variacao if vid_item else
                                       sku_item == r["SKU"].strip().casefold() and sku_item != "não informado")
                        if corresponde:
                            pedidos_sku.add(str(ped.get("id") or ped.get("number") or ""))
                pedidos_distintos = len(pedidos_sku)
                amostra_suficiente = historico_suficiente and pedidos_distintos >= 3 and unidades >= 5
                if saldo is None:
                    situacao = "Dados pendentes"
                    orientacao = "Verificar saldo e controle de estoque na Nuvemshop"
                elif not amostra_suficiente:
                    situacao = "Em observação"
                    orientacao = "Manter acompanhamento; não aumentar nem reduzir por este indicador"
                elif cobertura is not None and cobertura < 7:
                    situacao = "Prioridade de reposição"
                    orientacao = "Conferir demanda nos demais canais e prazo do fornecedor antes de comprar"
                elif cobertura is not None and cobertura <= cobertura_alvo:
                    situacao = "Manter / acompanhar"
                    orientacao = "Acompanhar evolução da cobertura e planejar reposição conforme prazo"
                else:
                    situacao = "Revisar capital imobilizado"
                    orientacao = "Revisar próximas compras; não reduzir estoque sem conferir outros canais"
                dados_decisao.append({
                    "Produto": r["Produto"], "SKU": r["SKU"], "Saldo": saldo,
                    "Vendido (un.)": float(unidades), "Pedidos com SKU": pedidos_distintos,
                    "Cobertura": (f"{cobertura:.1f} dias".replace(".", ",")
                                  if cobertura is not None else "Indeterminada"),
                    "Capital (R$)": moeda(r["Capital"]) if r["Capital"] is not None else "Pendente",
                    "Classificação": situacao, "Orientação": orientacao,
                })
            if not historico_suficiente:
                st.warning("Histórico comercial insuficiente: todos os SKUs com dados válidos permanecem em observação. Nenhuma recomendação de aumento ou redução de estoque será emitida nesta fase.")
            if dados_decisao:
                df_decisao = pd.DataFrame(dados_decisao)
                prioridades = ["Prioridade de reposição", "Manter / acompanhar", "Revisar capital imobilizado", "Em observação", "Dados pendentes"]
                filtro_decisao = st.selectbox("Filtrar classificação", ["Todas"] + prioridades, key="filtro_reposicao_maHUT")
                if filtro_decisao != "Todas":
                    df_decisao = df_decisao[df_decisao["Classificação"] == filtro_decisao]
                tabela_br(df_decisao, use_container_width=True, hide_index=True)
                st.caption("O pedido de teste #109 permanece na Nuvemshop; a opção de filtro controla sua participação nos cálculos deste módulo. A classificação é conservadora para não confundir uma compra isolada com demanda recorrente. A análise não modifica estoque, preços ou pedidos.")


            st.markdown("### Simulador de compras — por perfume e tamanho (SKU)")
            st.info("Simulação gerencial sem envio de pedidos ou alteração de saldos. Os custos são os valores atuais cadastrados na Nuvemshop; frete de compra, impostos adicionais e descontos de fornecedor não estão incluídos.")
            disponiveis_sim = [r for r in registros_estoque
                               if r["Saldo"] is not None and r["Custo unitário"] is not None
                               and r["ID variação"]]
            por_id_sim = {r["ID variação"]: r for r in disponiveis_sim}
            if len(por_id_sim) != len(disponiveis_sim):
                st.warning("Há identificadores de variação duplicados no catálogo; confira os cadastros antes de simular compras.")
            elif not disponiveis_sim:
                st.warning("Nenhum SKU com saldo e custo válidos está disponível para simulação.")
            else:
                if "carrinho_simulacao_maHUT" not in st.session_state:
                    st.session_state["carrinho_simulacao_maHUT"] = {}
                carrinho_sim = st.session_state["carrinho_simulacao_maHUT"]
                # Recalcular sempre com os dados atuais da API; não salvar custos no estado.
                for vid_antigo in list(carrinho_sim):
                    if vid_antigo not in por_id_sim:
                        del carrinho_sim[vid_antigo]
                ordenados_sim = sorted(disponiveis_sim, key=lambda r: (r["Produto"].casefold(), r["SKU"].casefold()))
                opcoes_sim = [r["ID variação"] for r in ordenados_sim]
                nomes_sim = {r["ID variação"]: f'{r["Produto"]} | SKU: {r["SKU"]} | saldo: {r["Saldo"]} un.'
                             for r in ordenados_sim}
                col_sku_sim, col_qtd_sim, col_add_sim = st.columns([5, 2, 2])
                with col_sku_sim:
                    vid_escolhido = st.selectbox("Perfume e tamanho", opcoes_sim,
                        format_func=lambda vid: nomes_sim[vid], key="sku_compra_sim_maHUT")
                with col_qtd_sim:
                    qtd_escolhida = st.number_input("Quantidade a comprar (un.)", min_value=1,
                        max_value=100000, value=1, step=1, key="qtd_compra_sim_maHUT")
                with col_add_sim:
                    st.write(" ")
                    st.write(" ")
                    if st.button("Adicionar à simulação", key="add_compra_sim_maHUT"):
                        carrinho_sim[vid_escolhido] = carrinho_sim.get(vid_escolhido, 0) + int(qtd_escolhida)
                        st.rerun()
                if carrinho_sim:
                    linhas_compra_sim = []
                    investimento_sim = Decimal("0")
                    unidades_compra_sim = 0
                    capital_atual_selecionado = Decimal("0")
                    capital_projetado_selecionado = Decimal("0")
                    mapa_giro_sim = {r["SKU"].casefold(): r for r in linhas_giro}
                    for vid, qtd in sorted(carrinho_sim.items(), key=lambda par: nomes_sim[par[0]]):
                        reg = por_id_sim[vid]
                        custo = reg["Custo unitário"]
                        saldo_atual = reg["Saldo"]
                        saldo_novo = saldo_atual + qtd
                        valor_compra = custo * qtd
                        capital_atual_item = custo * saldo_atual
                        capital_novo_item = custo * saldo_novo
                        investimento_sim += valor_compra
                        unidades_compra_sim += qtd
                        capital_atual_selecionado += capital_atual_item
                        capital_projetado_selecionado += capital_novo_item
                        # Identificar giro pela variação; não usar SKU como chave única.
                        giro_reg = next((g for g in linhas_giro if g["Produto"] == reg["Produto"]
                                         and g["SKU"] == reg["SKU"]), None)
                        media = giro_reg["Média diária"] if giro_reg else Decimal("0")
                        cobertura_atual = (Decimal(saldo_atual) / media if media > 0 else None)
                        cobertura_nova = (Decimal(saldo_novo) / media if media > 0 else None)
                        linhas_compra_sim.append({
                            "Produto": reg["Produto"], "SKU": reg["SKU"],
                            "Saldo atual": saldo_atual, "Comprar (un.)": qtd,
                            "Saldo projetado": saldo_novo,
                            "Custo unitário": moeda(custo),
                            "Investimento": moeda(valor_compra),
                            "Capital atual": moeda(capital_atual_item),
                            "Capital projetado": moeda(capital_novo_item),
                            "Cobertura atual": (f"{cobertura_atual:.1f} dias".replace(".", ",")
                                                 if cobertura_atual is not None else "Indeterminada"),
                            "Cobertura projetada": (f"{cobertura_nova:.1f} dias".replace(".", ",")
                                                    if cobertura_nova is not None else "Indeterminada"),
                        })
                    k1, k2, k3 = st.columns(3)
                    with k1: st.metric("Unidades simuladas", unidades_compra_sim)
                    with k2: st.metric("Investimento estimado", moeda(investimento_sim))
                    with k3: st.metric("Capital projetado — SKUs selecionados", moeda(capital_projetado_selecionado))
                    tabela_br(pd.DataFrame(linhas_compra_sim), use_container_width=True, hide_index=True)
                    st.caption(f"Capital atual dos SKUs selecionados: {moeda(capital_atual_selecionado)}. "
                               "Capital projetado = saldo atual mais quantidade simulada, multiplicado pelo custo unitário atual. "
                               "Cobertura calculada apenas com vendas pagas da loja virtual na janela selecionada; "
                               "não representa demanda total se o estoque atender outros canais.")
                    if not historico_suficiente:
                        st.warning("Histórico comercial insuficiente: a cobertura projetada é apenas um cenário matemático, não uma recomendação de compra.")
                    st.markdown("**Ajustar itens da simulação**")
                    vid_remover = st.selectbox("Selecionar item para retirar", list(carrinho_sim),
                        format_func=lambda vid: nomes_sim[vid], key="remover_sku_sim_maHUT")
                    rem_col, limpar_col = st.columns(2)
                    with rem_col:
                        if st.button("Retirar item", key="retirar_compra_sim_maHUT"):
                            del carrinho_sim[vid_remover]
                            st.rerun()
                    with limpar_col:
                        if st.button("Limpar simulação", key="limpar_compra_sim_maHUT"):
                            carrinho_sim.clear()
                            st.rerun()
                    csv_sim = pd.DataFrame(linhas_compra_sim).to_csv(index=False, sep=";").encode("utf-8-sig")
                    st.download_button("Exportar simulação em CSV", data=csv_sim,
                        file_name="MAHUT_simulacao_compras.csv", mime="text/csv",
                        key="exportar_compra_sim_maHUT")
                else:
                    st.caption("Adicione um ou mais perfumes e tamanhos para montar uma simulação de compra.")


            # ETAPA 5 — ORÇAMENTO E CONCENTRAÇÃO DO CAPITAL (SOMENTE LEITURA)
            st.markdown("### Planejamento financeiro do estoque — orçamento de compras")
            st.caption("O orçamento é um limite definido para esta simulação, não um saldo bancário nem uma autorização de compra. "
                       "Os custos são os valores atuais do catálogo e não incluem frete de aquisição, impostos adicionais ou descontos.")
            orcamento_sim = st.number_input(
                "Orçamento disponível para esta compra (R$)",
                min_value=0.0, max_value=100000000.0, value=1000.0, step=100.0,
                format="%.2f", key="orcamento_compras_maHUT")
            limite_compra = Decimal(str(orcamento_sim))
            carrinho_orc = st.session_state.get("carrinho_simulacao_maHUT", {})
            # Não pressupor que uma variação removida do catálogo ainda existe.
            investimento_orc = sum(
                (por_id_sim[vid]["Custo unitário"] * int(qtd)
                 for vid, qtd in carrinho_orc.items() if vid in por_id_sim),
                Decimal("0")) if disponiveis_sim and len(por_id_sim) == len(disponiveis_sim) else Decimal("0")
            restante_orc = limite_compra - investimento_orc
            comprometimento = (investimento_orc / limite_compra * 100) if limite_compra > 0 else None
            o1, o2, o3, o4 = st.columns(4)
            with o1: st.metric("Orçamento informado", moeda(limite_compra))
            with o2: st.metric("Compra simulada", moeda(investimento_orc))
            with o3: st.metric("Saldo do orçamento", moeda(restante_orc))
            with o4: st.metric("Orçamento comprometido", f"{comprometimento:.1f}%".replace(".", ",") if comprometimento is not None else "—")
            if investimento_orc > limite_compra:
                st.error(f"A simulação ultrapassa o orçamento em {moeda(investimento_orc - limite_compra)}. "
                         "Revise as quantidades antes de aprovar qualquer compra.")
            elif limite_compra == 0:
                st.info("Informe um orçamento maior que zero para comparar com a compra simulada.")
            else:
                st.progress(min(float(investimento_orc / limite_compra), 1.0), text="Uso do orçamento informado")
            if carrinho_orc and disponiveis_sim and len(por_id_sim) == len(disponiveis_sim):
                distribuicao = []
                for vid, qtd in carrinho_orc.items():
                    if vid not in por_id_sim:
                        continue
                    item = por_id_sim[vid]
                    investimento_item = item["Custo unitário"] * int(qtd)
                    distribuicao.append({
                        "Produto": item["Produto"], "SKU": item["SKU"],
                        "Quantidade": int(qtd), "Investimento (R$)": float(investimento_item),
                        "% da compra": round(float(investimento_item / investimento_orc * 100), 2) if investimento_orc else 0.0,
                    })
                if distribuicao:
                    st.markdown("**Distribuição do orçamento entre os SKUs simulados**")
                    tabela_br(pd.DataFrame(distribuicao).sort_values("Investimento (R$)", ascending=False),
                                 use_container_width=True, hide_index=True,
                                 column_config={"Investimento (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
                                                "% da compra": st.column_config.NumberColumn(format="%.2f%%")})
                    st.download_button("Exportar planejamento financeiro em CSV",
                        data=pd.DataFrame(distribuicao).to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                        file_name="MAHUT_planejamento_financeiro_estoque.csv", mime="text/csv",
                        key="csv_planejamento_maHUT")

            st.markdown("### Capital imobilizado por perfume e tamanho")
            st.caption("Participação de cada SKU no capital do estoque conhecido, calculada pelo saldo atual "
                       "multiplicado pelo custo unitário atual cadastrado na Nuvemshop. Não representa valor de mercado.")
            capital_validos = [r for r in registros_estoque if r["Capital"] is not None]
            capital_pendente = sum(1 for r in registros_estoque if r["Capital"] is None)
            capital_total_conhecido = sum((r["Capital"] for r in capital_validos), Decimal("0"))
            if capital_pendente:
                st.warning(f"{capital_pendente} SKU(s) sem saldo ou custo válido ficaram fora do cálculo. "
                           "As participações representam apenas o capital conhecido.")
            if capital_validos and capital_total_conhecido > 0:
                capital_linhas = []
                for reg in capital_validos:
                    capital_linhas.append({
                        "Produto": reg["Produto"], "SKU": reg["SKU"], "Saldo": reg["Saldo"],
                        "Capital (R$)": float(reg["Capital"]),
                        "Participação (%)": round(float(reg["Capital"] / capital_total_conhecido * 100), 2),
                    })
                capital_df = pd.DataFrame(capital_linhas).sort_values("Capital (R$)", ascending=False)
                st.metric("Capital total conhecido no estoque", moeda(capital_total_conhecido))
                tabela_br(capital_df, use_container_width=True, hide_index=True,
                             column_config={"Capital (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
                                            "Participação (%)": st.column_config.NumberColumn(format="%.2f%%")})
                top5 = capital_df.head(5)
                st.caption(f"Os cinco SKUs com maior capital representam {numero_br(top5['Participação (%)'].sum(), 1)}% "
                           "do capital conhecido. Concentração financeira não significa, por si só, excesso de estoque.")
                st.bar_chart(capital_df.head(10).set_index("SKU")[["Capital (R$)"]], horizontal=True)
            else:
                st.info("Ainda não há capital de estoque válido para analisar concentração.")
            # ETAPA 6 — CURVA ABC FINANCEIRA DO ESTOQUE POR SKU
            st.markdown("### Curva ABC financeira — capital investido por SKU")
            st.caption("Classificação pelo capital atual (saldo × custo unitário) das variações. "
                       "A: até 80% acumulados; B: de 80% a 95%; C: acima de 95%. "
                       "O SKU que atravessa um limite permanece na faixa que iniciou, "
                       "para não dividir uma variação entre classes. Não é uma curva ABC de vendas.")
            abc_elegiveis = [r for r in registros_estoque
                             if r["Capital"] is not None and r["Capital"] > 0]
            abc_sem_capital = sum(1 for r in registros_estoque if r["Capital"] is None)
            abc_zerados = sum(1 for r in registros_estoque if r["Capital"] is not None and r["Capital"] == 0)
            abc_total = sum((r["Capital"] for r in abc_elegiveis), Decimal("0"))
            if abc_sem_capital:
                st.warning(f"{abc_sem_capital} SKU(s) sem saldo/custo válido não entram na curva ABC.")
            if abc_zerados:
                st.caption(f"{abc_zerados} SKU(s) com capital zero não entram na curva ABC financeira; "
                           "continuam disponíveis na tabela geral de estoque.")
            if abc_total > 0:
                abc_ordenados = sorted(abc_elegiveis,
                                      key=lambda r: (-r["Capital"], r["Produto"].casefold(), r["SKU"].casefold()))
                abc_linhas = []
                abc_acumulado = Decimal("0")
                for reg in abc_ordenados:
                    antes = abc_acumulado / abc_total * 100
                    classe = "A" if antes < 80 else "B" if antes < 95 else "C"
                    abc_acumulado += reg["Capital"]
                    abc_linhas.append({
                        "Produto": reg["Produto"], "SKU": reg["SKU"], "Classe": classe,
                        "Saldo": reg["Saldo"], "Capital (R$)": float(reg["Capital"]),
                        "Participação (%)": round(float(reg["Capital"] / abc_total * 100), 2),
                        "Acumulado (%)": round(float(abc_acumulado / abc_total * 100), 2),
                    })
                abc_df = pd.DataFrame(abc_linhas)
                abc_resumo = abc_df.groupby("Classe", as_index=False).agg(
                    **{"SKUs": ("SKU", "count"), "Capital (R$)": ("Capital (R$)", "sum")})
                abc_resumo["Participação (%)"] = abc_resumo["Capital (R$)"] / float(abc_total) * 100
                abc_resumo = abc_resumo.set_index("Classe").reindex(["A", "B", "C"], fill_value=0).reset_index()
                abc_resumo["Participação (%)"] = abc_resumo["Participação (%)"].round(2)
                st.markdown("**Distribuição do capital por classe**")
                ca, cb, cc = st.columns(3)
                for coluna, classe in zip((ca, cb, cc), ("A", "B", "C")):
                    item = abc_resumo.loc[abc_resumo["Classe"] == classe].iloc[0]
                    with coluna:
                        st.metric(f"Classe {classe} — {int(item['SKUs'])} SKUs",
                                  moeda(Decimal(str(item["Capital (R$)"]))))
                        st.caption(f"{item['Participação (%)']:.2f}% do capital analisado".replace(".", ","))
                # Formatação brasileira apenas na exibição; cálculos permanecem numéricos.
                abc_resumo_exibicao = abc_resumo.copy()
                abc_resumo_exibicao["Capital (R$)"] = abc_resumo_exibicao["Capital (R$)"].map(moeda)
                tabela_br(abc_resumo_exibicao, use_container_width=True, hide_index=True,
                             column_config={"Participação (%)": st.column_config.NumberColumn(format="%.2f%%")})
                st.markdown("**Curva de Pareto — capital acumulado por SKU**")
                abc_grafico = abc_df.copy()
                abc_grafico["Ordem"] = range(1, len(abc_grafico) + 1)
                abc_grafico["Capital exibido"] = abc_grafico["Capital (R$)"].map(moeda)
                abc_grafico["Acumulado exibido"] = abc_grafico["Acumulado (%)"].map(lambda x: numero_br(x, 2) + "%")
                abc_base = alt.Chart(abc_grafico).encode(
                    x=alt.X("Ordem:Q", title="SKUs ordenados por capital (maior para menor)",
                            scale=alt.Scale(domain=[1, max(2, len(abc_grafico))])),
                    tooltip=["Produto:N", "SKU:N", "Classe:N",
                             alt.Tooltip("Capital exibido:N", title="Capital (R$)"),
                             alt.Tooltip("Acumulado exibido:N", title="Acumulado (%)")])
                abc_curva = abc_base.mark_line(point=True).encode(
                    y=alt.Y("Acumulado (%):Q", title="Capital acumulado (%)", scale=alt.Scale(domain=[0, 100])))
                abc_limites = alt.Chart(pd.DataFrame({"Limite": [80, 95]})).mark_rule(
                    color="#888888", strokeDash=[5, 5]).encode(y="Limite:Q")
                st.altair_chart((abc_curva + abc_limites).properties(height=330), use_container_width=True)
                abc_filtro = st.selectbox("Filtrar a curva ABC", ["Todas", "A", "B", "C"],
                                         key="filtro_abc_estoque_maHUT")
                abc_exibicao = abc_df if abc_filtro == "Todas" else abc_df[abc_df["Classe"] == abc_filtro]
                abc_exibicao_formatada = abc_exibicao.copy()
                abc_exibicao_formatada["Capital (R$)"] = abc_exibicao_formatada["Capital (R$)"].map(moeda)
                tabela_br(abc_exibicao_formatada, use_container_width=True, hide_index=True,
                             column_config={"Participação (%)": st.column_config.NumberColumn(format="%.2f%%"),
                                            "Acumulado (%)": st.column_config.NumberColumn(format="%.2f%%")})
                st.download_button("Exportar Curva ABC em CSV",
                                   data=abc_df.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                                   file_name="MAHUT_curva_ABC_financeira_estoque.csv", mime="text/csv",
                                   key="csv_abc_financeira_maHUT")
                st.info("Classe A significa maior concentração de capital, não maior demanda. "
                        "Não reduzir nem aumentar compras apenas pela classificação ABC; "
                        "o histórico da loja virtual ainda é curto e o estoque pode atender outros canais.")
            else:
                st.info("Não há SKUs com capital positivo conhecido para calcular a Curva ABC.")

            # ETAPA 7 — ABC DE FATURAMENTO × ABC FINANCEIRA DO ESTOQUE
            st.markdown("### Curva ABC de vendas × estoque — análise preliminar")
            st.info("Cruzamento gerencial por SKU: classe financeira = capital em estoque; "
                    "classe comercial = receita de mercadorias atribuída ao SKU em pedidos pagos. "
                    "A loja tem pouco histórico de divulgação e o estoque pode atender outros canais. "
                    "Nenhuma classificação é uma ordem de compra ou redução.")
            periodo_cruz = st.selectbox("Período das vendas para o cruzamento",
                                        [7, 15, 30, 60, 90, "Todos os pedidos"], index=2,
                                        format_func=lambda v: f"Últimos {v} dias" if isinstance(v, int) else v,
                                        key="periodo_abc_cruzamento_maHUT")
            hoje_cruz = datetime.now().date()
            inicio_cruz = (hoje_cruz - timedelta(days=periodo_cruz - 1)
                           if isinstance(periodo_cruz, int) else datetime(2000, 1, 1).date())
            pedidos_cruz = filtrar_pedidos_dre(pedidos_giro, inicio_cruz, hoje_cruz)
            taxas_cruz = st.session_state.get("taxas_reais_maHUT", {
                numero: valor_br_sem_simbolo(valor) for numero, valor in TAXAS_REAIS_CONFERIDAS.items()})
            linhas_vendas_cruz, alertas_vendas_cruz = linhas_rentabilidade_produtos(
                pedidos_cruz, catalogo_estoque, taxas_cruz)
            vendas_por_sku = {}
            unidades_por_sku = {}
            pedidos_por_sku = {}
            sem_sku_cruz = 0
            for item_cruz in linhas_vendas_cruz:
                sku_cruz = str(item_cruz.get("SKU") or "").strip().casefold()
                if not sku_cruz:
                    sem_sku_cruz += 1
                    continue
                vendas_por_sku[sku_cruz] = (vendas_por_sku.get(sku_cruz, Decimal("0"))
                                             + item_cruz["Receita"])
                unidades_por_sku[sku_cruz] = (unidades_por_sku.get(sku_cruz, Decimal("0"))
                                               + item_cruz["Unidades"])
                pedidos_por_sku.setdefault(sku_cruz, set()).add(item_cruz["Pedido"])
            abc_vendas_classes, abc_vendas_part, _, total_vendas_abc = classificar_abc_maHUT(
                vendas_por_sku.items())
            abc_estoque_classes, _, _, _ = classificar_abc_maHUT(
                [(r["ID variação"], r["Capital"]) for r in registros_estoque])
            skus_catalogo = {}
            for r in registros_estoque:
                chave = r["SKU"].strip().casefold()
                if chave and chave != "não informado":
                    skus_catalogo.setdefault(chave, []).append(r)
            linhas_cruz = []
            for reg in registros_estoque:
                sku = reg["SKU"].strip().casefold()
                duplicado = len(skus_catalogo.get(sku, [])) > 1
                receita = vendas_por_sku.get(sku, Decimal("0"))
                if duplicado:
                    classe_vendas = "SKU duplicado — conferir"
                    receita_exibida = None
                    unidades_exibidas = None
                    pedidos_exibidos = None
                elif not sku or sku == "não informado":
                    classe_vendas = "SKU não informado"
                    receita_exibida = None
                    unidades_exibidas = None
                    pedidos_exibidos = None
                else:
                    classe_vendas = abc_vendas_classes.get(sku, "Sem venda identificada")
                    receita_exibida = receita
                    unidades_exibidas = unidades_por_sku.get(sku, Decimal("0"))
                    pedidos_exibidos = len(pedidos_por_sku.get(sku, set()))
                classe_estoque = (abc_estoque_classes.get(reg["ID variação"], "Sem capital positivo")
                                   if reg["Capital"] is not None else "Capital pendente")
                if classe_estoque == "A" and classe_vendas in ("B", "C", "Sem venda identificada"):
                    leitura = "Alto capital; demanda online a acompanhar"
                elif classe_vendas == "A" and classe_estoque in ("B", "C", "Sem capital positivo"):
                    leitura = "Destaque de vendas online; acompanhar saldo"
                elif classe_estoque == "A" and classe_vendas == "A":
                    leitura = "Capital e vendas online relevantes"
                else:
                    leitura = "Monitorar — histórico inicial"
                linhas_cruz.append({
                    "Produto": reg["Produto"], "SKU": reg["SKU"],
                    "ABC estoque": classe_estoque, "ABC vendas": classe_vendas,
                    "Saldo (un.)": reg["Saldo"],
                    "Capital (R$)": reg["Capital"],
                    "Receita online (R$)": receita_exibida,
                    "Unidades vendidas": unidades_exibidas,
                    "Pedidos com SKU": pedidos_exibidos,
                    "Leitura gerencial": leitura,
                })
            st.caption("ABC comercial baseada na receita atribuída aos itens dos pedidos pagos; "
                       "não inclui frete e pode refletir descontos rateados. "
                       "O filtro do pedido de teste #109 segue a opção escolhida no início do módulo. "
                       "Sem venda identificada não significa ausência de demanda em outros canais.")
            if sem_sku_cruz:
                st.warning(f"{inteiro_br(sem_sku_cruz)} item(ns) vendido(s) sem SKU não foram atribuídos ao cruzamento.")
            if alertas_vendas_cruz:
                st.warning(f"Há {inteiro_br(len(alertas_vendas_cruz))} aviso(s) de conciliação na base de vendas. "
                           "Consulte o Módulo 3 para conferir eventuais pendências.")
            ccr1, ccr2, ccr3, ccr4 = st.columns(4)
            with ccr1:
                st.metric("Receita analisada por SKU", moeda(total_vendas_abc))
            with ccr2:
                st.metric("SKUs com receita online", inteiro_br(len(abc_vendas_classes)))
            with ccr3:
                st.metric("SKUs na classe A de vendas", inteiro_br(sum(c == "A" for c in abc_vendas_classes.values())))
            with ccr4:
                st.metric("SKUs com capital A e vendas B/C ou ausentes",
                          inteiro_br(sum(x["ABC estoque"] == "A" and x["ABC vendas"] in
                                         ("B", "C", "Sem venda identificada") for x in linhas_cruz)))
            if not abc_vendas_classes:
                st.warning("Nenhuma receita positiva atribuída a SKU neste período. "
                           "A classificação comercial ainda não pode ser construída.")
            else:
                st.markdown("**Distribuição da receita online por classe ABC**")
                resumo_vendas = []
                for classe in ("A", "B", "C"):
                    chaves = [k for k, c in abc_vendas_classes.items() if c == classe]
                    valor = sum((vendas_por_sku[k] for k in chaves), Decimal("0"))
                    resumo_vendas.append({"Classe": classe, "SKUs": len(chaves),
                                          "Receita (R$)": valor,
                                          "Participação (%)": valor / total_vendas_abc * 100})
                tabela_br(pd.DataFrame(resumo_vendas), use_container_width=True, hide_index=True)
            filtro_cruz = st.selectbox("Filtrar cruzamento de vendas e estoque",
                                      ["Todos", "Alto capital e vendas menores/ausentes",
                                       "Vendas classe A", "Estoque classe A", "Sem venda identificada"],
                                      key="filtro_cruz_abc_maHUT")
            linhas_filtradas = linhas_cruz
            if filtro_cruz == "Alto capital e vendas menores/ausentes":
                linhas_filtradas = [x for x in linhas_cruz if x["ABC estoque"] == "A"
                                   and x["ABC vendas"] in ("B", "C", "Sem venda identificada")]
            elif filtro_cruz == "Vendas classe A":
                linhas_filtradas = [x for x in linhas_cruz if x["ABC vendas"] == "A"]
            elif filtro_cruz == "Estoque classe A":
                linhas_filtradas = [x for x in linhas_cruz if x["ABC estoque"] == "A"]
            elif filtro_cruz == "Sem venda identificada":
                linhas_filtradas = [x for x in linhas_cruz if x["ABC vendas"] == "Sem venda identificada"]
            linhas_filtradas = sorted(linhas_filtradas,
                                     key=lambda x: (-(x["Capital (R$)"] or Decimal("0")), x["SKU"]))
            tabela_br(pd.DataFrame(linhas_filtradas), use_container_width=True, hide_index=True)
            # Exportação numérica, com decimal brasileiro, para manter os dados reutilizáveis.
            df_exportar_cruz = pd.DataFrame(linhas_cruz)
            st.download_button("Exportar cruzamento ABC em CSV",
                               data=df_exportar_cruz.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                               file_name="MAHUT_ABC_vendas_estoque.csv", mime="text/csv",
                               key="exportar_cruz_abc_maHUT")
            st.warning("As vendas observadas não refletem necessariamente toda a demanda da MAHUT. "
                       "Não reduzir ou aumentar estoque automaticamente com poucos dias de divulgação.")


            # ETAPA 8 — PAINEL DE ALERTAS GERENCIAIS (LEITURA E EXPORTAÇÃO)
            st.markdown("### Painel de alertas gerenciais — Etapa 8")
            st.caption("Central de alertas por perfume e tamanho (SKU). Uma mesma variação pode gerar "
                       "mais de um alerta; os totais são ocorrências, não perfumes distintos. "
                       "As classificações comerciais usam o período escolhido no cruzamento ABC acima.")
            alertas_gerenciais, historico_apto_alertas = gerar_alertas_gerenciais_maHUT(
                registros_estoque, linhas_cruz, linhas_giro,
                dias_operacao, dias_minimos, janela_giro)
            tipos_gerenciais = ["Estoque zerado", "Cobertura baixa", "Capital elevado",
                                "Alto capital / poucas vendas", "Cadastro / saldo",
                                "Cadastro / custo", "Cadastro / SKU"]
            contagem_por_tipo = {tipo: sum(a["Tipo de alerta"] == tipo for a in alertas_gerenciais)
                                 for tipo in tipos_gerenciais}
            ag1, ag2, ag3, ag4 = st.columns(4)
            with ag1: st.metric("Estoque zerado", inteiro_br(contagem_por_tipo["Estoque zerado"]))
            with ag2: st.metric("Cobertura baixa (validada)", inteiro_br(contagem_por_tipo["Cobertura baixa"]))
            with ag3: st.metric("Capital elevado — Top 10", inteiro_br(contagem_por_tipo["Capital elevado"]))
            with ag4: st.metric("Alto capital / vendas menores", inteiro_br(contagem_por_tipo["Alto capital / poucas vendas"]))
            cadastro_pend = sum(contagem_por_tipo[t] for t in ("Cadastro / saldo", "Cadastro / custo", "Cadastro / SKU"))
            st.caption(f"Pendências cadastrais identificadas: {inteiro_br(cadastro_pend)} ocorrência(s). "
                       "O mesmo SKU pode aparecer em diferentes categorias.")
            if not historico_apto_alertas:
                st.warning("Histórico de divulgação ou janela de giro insuficiente: "
                           "alertas de cobertura baixa ficam suspensos. "
                           "Alertas de capital versus vendas são apenas observações preliminares.")
            else:
                st.info("Cobertura baixa exige também pelo menos 3 pedidos com SKU e 5 unidades "
                        "vendidas na janela. A cobertura considera somente pedidos pagos da Nuvemshop.")
            f1, f2 = st.columns(2)
            with f1:
                tipo_alerta_filtro = st.selectbox("Tipo de alerta", ["Todos"] + tipos_gerenciais,
                                                  key="tipo_alerta_gerencial_maHUT")
            with f2:
                nivel_alerta_filtro = st.selectbox("Nível de atenção",
                                                   ["Todos", "Crítico", "Atenção", "Observação", "Informativo"],
                                                   key="nivel_alerta_gerencial_maHUT")
            busca_alerta = st.text_input("Buscar perfume ou SKU nos alertas", key="busca_alerta_gerencial_maHUT")
            alertas_visiveis = [a for a in alertas_gerenciais
                               if (tipo_alerta_filtro == "Todos" or a["Tipo de alerta"] == tipo_alerta_filtro)
                               and (nivel_alerta_filtro == "Todos" or a["Nível"] == nivel_alerta_filtro)
                               and (not busca_alerta or busca_alerta.casefold() in
                                    (a["Produto"] + " " + a["SKU"]).casefold())]
            st.caption(f"{inteiro_br(len(alertas_visiveis))} alerta(s) exibido(s) "
                       f"de {inteiro_br(len(alertas_gerenciais))} ocorrência(s) identificada(s).")
            colunas_alertas = ["Nível", "Tipo de alerta", "Produto", "SKU", "Saldo (un.)",
                               "Capital (R$)", "Detalhe", "Próxima verificação", "Natureza"]
            if alertas_visiveis:
                tabela_br(pd.DataFrame(alertas_visiveis, columns=colunas_alertas),
                          use_container_width=True, hide_index=True)
            else:
                st.info("Nenhum alerta encontrado com os filtros selecionados.")
            # Exportação de valores numéricos, em CSV com ponto e vírgula e vírgula decimal.
            csv_alertas = pd.DataFrame(alertas_visiveis, columns=colunas_alertas).to_csv(
                index=False, sep=";", decimal=",", na_rep="")
            st.download_button("Exportar alertas filtrados em CSV",
                               data=("\ufeff" + csv_alertas).encode("utf-8"),
                               file_name="MAHUT_alertas_gerenciais_estoque.csv",
                               mime="text/csv", key="csv_alertas_gerenciais_maHUT")
            st.info("Painel somente leitura: não cria pedidos de compra, não modifica o estoque "
                    "da Nuvemshop/Bling e não remove o pedido de teste #109.")

            # ETAPA 9 — CENÁRIOS DE DEMANDA E REPOSIÇÃO (SEM ALTERAR A LOJA)
            st.markdown("### Previsão de demanda e reposição — Etapa 9")
            st.info("Projeção experimental baseada exclusivamente em pedidos pagos da Nuvemshop. "
                    "Não inclui vendas do showroom, Shopee ou baixas externas do Bling. "
                    "Os cenários não são previsão estatística nem pedido de compra.")
            p9a, p9b, p9c, p9d = st.columns(4)
            with p9a:
                horizonte_p9 = st.selectbox("Horizonte (dias)", [15, 30, 60, 90], index=1,
                                            key="horizonte_demanda_maHUT")
            with p9b:
                prazo_p9 = st.number_input("Prazo do fornecedor (dias)", 0, 180, 15, 1,
                                           key="prazo_fornecedor_maHUT")
            with p9c:
                seguranca_p9 = st.number_input("Estoque de segurança (dias)", 0, 90, 7, 1,
                                               key="seguranca_demanda_maHUT")
            with p9d:
                cenario_p9 = st.selectbox("Cenário", ["Conservador (−20%)", "Referência", "Crescimento (+20%)"],
                                          index=1, key="cenario_demanda_maHUT")
            fator_p9 = {"Conservador (−20%)": Decimal("0.8"),
                        "Referência": Decimal("1"),
                        "Crescimento (+20%)": Decimal("1.2")}[cenario_p9]
            # Não diluir a média por dias anteriores ao início da divulgação ativa informada.
            dias_base_p9 = max(1, min(int(janela_giro), int(dias_operacao)))
            st.caption(f"Base de cálculo: unidades de pedidos pagos nos últimos {janela_giro} dias, "
                       f"divididas por {dias_base_p9} dia(s) de divulgação ativa informados. "
                       "A data real de início da divulgação não é verificada pela API; ajuste esse parâmetro "
                       f"com cuidado. Pedido de teste #109 {'desconsiderado' if excluir_teste_109 else 'incluído'} nos cálculos.")
            # A etapa 3 já conta pedidos distintos por SKU; reutilizamos essa verificação.
            contagens_p9 = {(str(r["Produto"]), str(r["SKU"])): r["Pedidos com SKU"]
                            for r in dados_decisao}
            registros_p9 = {(r["Produto"], r["SKU"]): r for r in registros_estoque}
            saida_p9 = []
            for linha_p9 in linhas_giro:
                chave_p9 = (linha_p9["Produto"], linha_p9["SKU"])
                cadastro_p9 = registros_p9.get(chave_p9, {})
                saldo_p9 = linha_p9["Saldo"]
                unidades_p9 = linha_p9["Unidades vendidas"]
                pedidos_p9 = contagens_p9.get(chave_p9, 0)
                media_p9 = unidades_p9 / Decimal(dias_base_p9)
                media_cenario_p9 = media_p9 * fator_p9
                amostra_p9 = (historico_suficiente and pedidos_p9 >= 3 and unidades_p9 >= 5)
                if saldo_p9 is None:
                    status_p9 = "Saldo pendente"
                elif not amostra_p9:
                    status_p9 = "Experimental — histórico insuficiente"
                else:
                    status_p9 = "Amostra mínima atendida — validar outros canais"
                if saldo_p9 is not None:
                    demanda_p9 = int((media_cenario_p9 * Decimal(horizonte_p9)).to_integral_value(rounding="ROUND_CEILING"))
                    alvo_p9 = int((media_cenario_p9 * Decimal(prazo_p9 + seguranca_p9)).to_integral_value(rounding="ROUND_CEILING"))
                    sugestao_p9 = max(0, alvo_p9 - saldo_p9)
                    saldo_futuro_p9 = saldo_p9 - demanda_p9
                    cobertura_p9 = (Decimal(saldo_p9) / media_cenario_p9
                                    if media_cenario_p9 > 0 else None)
                else:
                    demanda_p9 = alvo_p9 = sugestao_p9 = saldo_futuro_p9 = cobertura_p9 = None
                custo_p9 = cadastro_p9.get("Custo unitário")
                saida_p9.append({
                    "Produto": linha_p9["Produto"], "SKU": linha_p9["SKU"],
                    "Saldo atual (un.)": saldo_p9, "Vendas na janela (un.)": unidades_p9,
                    "Pedidos com SKU": pedidos_p9,
                    "Demanda no horizonte (un.)": demanda_p9,
                    "Saldo projetado (un.)": saldo_futuro_p9,
                    "Cobertura cenário (dias)": cobertura_p9,
                    "Reposição simulada (un.)": sugestao_p9,
                    "Investimento simulado (R$)": (custo_p9 * sugestao_p9
                                                   if custo_p9 is not None and sugestao_p9 is not None else None),
                    "Confiabilidade": status_p9,
                })
            df_p9 = pd.DataFrame(saida_p9)
            amostra_valida_p9 = sum(x["Confiabilidade"].startswith("Amostra mínima") for x in saida_p9)
            proj_p9 = sum(x["Reposição simulada (un.)"] or 0 for x in saida_p9)
            c9a, c9b, c9c = st.columns(3)
            with c9a: st.metric("SKUs com amostra mínima", inteiro_br(amostra_valida_p9))
            with c9b: st.metric("SKUs analisados", inteiro_br(len(saida_p9)))
            with c9c: st.metric("Unidades de reposição simuladas", inteiro_br(proj_p9))
            if not historico_suficiente:
                st.warning("Histórico insuficiente: TODAS as quantidades de reposição abaixo "
                           "são exercícios de cenário, não recomendações de compra. "
                           "O parâmetro de dias ativos não comprova a continuidade das vendas.")
            st.caption("Reposição simulada = máximo entre zero e [demanda diária do cenário × "
                       "(prazo do fornecedor + segurança) − saldo atual], arredondando unidades para cima. "
                       "Não considera pedidos em trânsito, lotes mínimos, promoções futuras nem outros canais. "
                       "Saldo projetado pode ser negativo para indicar falta hipotética; não altera estoque real.")
            filtro_p9 = st.selectbox("Filtrar análise de demanda", ["Todos", "Somente com venda online",
                "Somente reposição simulada positiva", "Somente amostra mínima atendida"],
                key="filtro_demanda_maHUT")
            busca_p9 = st.text_input("Buscar perfume ou SKU na projeção", key="busca_demanda_maHUT")
            visiveis_p9 = [r for r in saida_p9
                if (not busca_p9 or busca_p9.casefold() in (r["Produto"] + " " + r["SKU"]).casefold())
                and (filtro_p9 == "Todos" or
                     (filtro_p9 == "Somente com venda online" and r["Vendas na janela (un.)"] > 0) or
                     (filtro_p9 == "Somente reposição simulada positiva" and (r["Reposição simulada (un.)"] or 0) > 0) or
                     (filtro_p9 == "Somente amostra mínima atendida" and r["Confiabilidade"].startswith("Amostra mínima")))]
            visiveis_p9.sort(key=lambda r: (-(r["Reposição simulada (un.)"] or 0), r["Produto"], r["SKU"]))
            if visiveis_p9:
                tabela_br(pd.DataFrame(visiveis_p9), use_container_width=True, hide_index=True)
            else:
                st.info("Nenhum SKU corresponde aos filtros selecionados.")
            st.download_button("Exportar simulação de demanda em CSV",
                data=("\ufeff" + pd.DataFrame(visiveis_p9, columns=list(df_p9.columns)).to_csv(
                    index=False, sep=";", decimal=",", na_rep="")).encode("utf-8"),
                file_name="MAHUT_previsao_demanda_etapa9.csv", mime="text/csv",
                key="csv_previsao_demanda_maHUT")
            st.info("Etapa 9 somente leitura. Não cria pedidos, não atualiza Nuvemshop/Bling "
                    "e não remove o pedido de teste #109. O filtro comercial não modifica a loja.")

            # ETAPA 10 — PAINEL EXECUTIVO E CONSOLIDAÇÃO DO ESTOQUE
            st.markdown("### Painel executivo de estoque — Etapa 10")
            st.caption("Consolidação dos dados reais de saldo e capital da Nuvemshop e dos indicadores "
                       "comerciais da janela selecionada. Os alertas podem se sobrepor; projeções são experimentais.")
            k10a, k10b, k10c, k10d = st.columns(4)
            with k10a:
                st.metric("Saldo conhecido (un.)", inteiro_br(saldo_total))
            with k10b:
                st.metric("Capital identificado", moeda(capital_total))
            with k10c:
                st.metric("SKUs zerados", inteiro_br(zerados))
            with k10d:
                st.metric("Pendências cadastrais", inteiro_br(cadastro_pend))
            k10e, k10f, k10g, k10h = st.columns(4)
            with k10e:
                st.metric("Pedidos pagos analisados", inteiro_br(qtd_pedidos_giro))
            with k10f:
                st.metric("SKUs com venda na janela", inteiro_br(itens_com_venda))
            with k10g:
                st.metric("Alertas registrados", inteiro_br(len(alertas_gerenciais)))
            with k10h:
                st.metric("Amostra mínima — demanda", inteiro_br(amostra_valida_p9))
            if pendentes_estoque:
                st.warning(f"Capital parcial: {inteiro_br(pendentes_estoque)} variação(ões) "
                           "não possuem saldo ou custo suficiente para valorização.")
            st.info("Pedido de teste #109: " +
                    ("desconsiderado dos cálculos comerciais deste módulo; permanece na Nuvemshop."
                     if excluir_teste_109 else
                     "incluído nos cálculos comerciais. Ative o filtro no início do módulo para desconsiderá-lo."))
            if not amostra_valida_p9:
                st.warning("Nenhum SKU possui amostra mínima para previsão de demanda. "
                           "Reposições simuladas não são recomendações de compra.")
            st.markdown("**Prioridades de acompanhamento**")
            prioridades10 = []
            for r in registros_estoque:
                if r["Saldo"] == 0:
                    prioridades10.append({"Prioridade": "Conferir disponibilidade",
                                         "Produto": r["Produto"], "SKU": r["SKU"],
                                         "Saldo (un.)": r["Saldo"], "Capital (R$)": r["Capital"],
                                         "Motivo": "Estoque controlado zerado"})
            for r in sorted((x for x in registros_estoque if x["Capital"] is not None and x["Capital"] > 0),
                            key=lambda x: x["Capital"], reverse=True)[:10]:
                prioridades10.append({"Prioridade": "Acompanhar capital",
                                     "Produto": r["Produto"], "SKU": r["SKU"],
                                     "Saldo (un.)": r["Saldo"], "Capital (R$)": r["Capital"],
                                     "Motivo": "Entre os 10 maiores valores de estoque"})
            filtro_prioridade10 = st.selectbox("Visualizar prioridades", ["Todas", "Conferir disponibilidade", "Acompanhar capital"],
                                                key="prioridades_executivo_maHUT")
            prioridades_visiveis10 = [x for x in prioridades10
                                      if filtro_prioridade10 == "Todas" or x["Prioridade"] == filtro_prioridade10]
            if prioridades_visiveis10:
                tabela_br(pd.DataFrame(prioridades_visiveis10), use_container_width=True, hide_index=True)
            else:
                st.success("Nenhuma prioridade nesta categoria.")
            st.download_button("Exportar resumo executivo em CSV",
                data=("\ufeff" + pd.DataFrame(prioridades_visiveis10,
                    columns=["Prioridade", "Produto", "SKU", "Saldo (un.)", "Capital (R$)", "Motivo"])
                    .to_csv(index=False, sep=";", decimal=",", na_rep="")).encode("utf-8"),
                file_name="MAHUT_painel_executivo_estoque_etapa10.csv",
                mime="text/csv", key="csv_executivo_estoque_maHUT")
            st.caption("A mesma variação pode aparecer em duas prioridades. O painel é somente leitura. "
                       "O filtro #109 se aplica às análises comerciais do Módulo 4, não aos outros módulos do BI. "
                       "Em tabelas largas, use a barra de rolagem horizontal para ver as últimas colunas.")

            st.warning("A análise financeira não é recomendação automática de reposição ou redução. "
                       "Com poucos dias de divulgação, avalie também sazonalidade, outros canais e estoque compartilhado.")


    except Exception as erro:
        st.error("Não foi possível consultar o estoque real da Nuvemshop. Confira a integração em Configurações.")
        st.caption(f"Detalhe técnico: {type(erro).__name__}: {erro}")

elif pagina == "5 · Financeiro":
    st.markdown("## Financeiro — loja virtual MAHUT")
    st.caption("Fonte: pedidos reais da Nuvemshop. Vendas do showroom e transferências bancárias não entram neste módulo.")
    st.info("CPV usa custos atuais da Nuvemshop. O custo de envio informado no pedido é provisório e não comprova o débito da etiqueta. Taxas de pagamento estimadas, sem conciliação real, e custos históricos ainda pendentes; não exibimos lucro líquido.")

    if st.button("Atualizar dados financeiros agora"):
        consultar_pedidos_nuvemshop.clear()
        consultar_produtos_nuvemshop_diagnostico.clear()
        st.rerun()
    try:
        pedidos_fin = consultar_pedidos_nuvemshop()
        base_fin = consolidar_vendas(pedidos_fin)
        st.markdown("### Período de análise")
        periodo_fin = st.selectbox("Selecionar período financeiro", [
            "Todos os pedidos", "Hoje", "Últimos 7 dias", "Últimos 30 dias",
            "Este mês", "Período personalizado",
        ], key="periodo_financeiro")
        hoje_fin = datetime.now().date()
        inicio_fin = fim_fin = None
        if periodo_fin == "Hoje":
            inicio_fin = fim_fin = hoje_fin
        elif periodo_fin == "Últimos 7 dias":
            inicio_fin, fim_fin = hoje_fin - timedelta(days=6), hoje_fin
        elif periodo_fin == "Últimos 30 dias":
            inicio_fin, fim_fin = hoje_fin - timedelta(days=29), hoje_fin
        elif periodo_fin == "Este mês":
            inicio_fin, fim_fin = hoje_fin.replace(day=1), hoje_fin
        elif periodo_fin == "Período personalizado":
            intervalo_fin = st.date_input(
                "Data inicial / final", value=(hoje_fin - timedelta(days=29), hoje_fin),
                format="DD/MM/YYYY", key="datas_financeiro")
            if isinstance(intervalo_fin, (tuple, list)) and len(intervalo_fin) == 2:
                inicio_fin, fim_fin = intervalo_fin
            else:
                st.info("Selecione as duas datas para aplicar o filtro.")
                inicio_fin, fim_fin = hoje_fin, hoje_fin - timedelta(days=1)
        if inicio_fin is not None:
            base_fin = base_fin[
                base_fin["Data"].notna() &
                (base_fin["Data"] >= inicio_fin) &
                (base_fin["Data"] <= fim_fin)
            ].copy()

        pagos_fin = base_fin[base_fin["Pagamento"] == "Pago"].copy()
        sem_valor_fin = int(pagos_fin["_total"].isna().sum())
        receita_fin = sum((v for v in pagos_fin["_total"] if v is not None), Decimal("0"))
        frete_fin = sum((v for v in pagos_fin["_frete"] if v is not None), Decimal("0"))
        sem_frete_fin = int(pagos_fin["_frete"].isna().sum())
        imposto_fin = receita_fin * Decimal("0.04")
        apos_imposto_fin = receita_fin - imposto_fin

        produtos_fin = consultar_produtos_nuvemshop_diagnostico()
        linhas_cpv, pendencias_cpv = cpv_pedidos(pedidos_fin, produtos_fin)
        chaves_filtradas = set(pagos_fin["Pedido"].astype(str).tolist())
        linhas_cpv = [r for r in linhas_cpv if r["Pedido"] in chaves_filtradas]
        pendencias_cpv = [r for r in pendencias_cpv if r["Pedido"] in chaves_filtradas]
        cpv_completo = bool(linhas_cpv) and all(r["Completo"] for r in linhas_cpv)
        cpv_total = sum((r["CPV"] for r in linhas_cpv if r["CPV"] is not None), Decimal("0"))
        cpv_disponivel = cpv_completo and len(linhas_cpv) == len(pagos_fin)
        resultado_preliminar = receita_fin - imposto_fin - cpv_total if cpv_disponivel else None
        # Campo informado pela API, não necessariamente o débito efetivo da etiqueta.
        custo_envio_por_pedido = {}
        for pedido in pedidos_fin:
            numero = str(pedido.get("number") or pedido.get("id") or "—")
            if numero in chaves_filtradas:
                custo_envio_por_pedido[numero] = valor_monetario(pedido.get("shipping_cost_owner"))
        custo_envio_completo = all(custo_envio_por_pedido.get(numero) is not None for numero in chaves_filtradas)
        custo_envio_total = sum((v for v in custo_envio_por_pedido.values() if v is not None), Decimal("0"))
        resultado_com_envio = (resultado_preliminar - custo_envio_total
                               if resultado_preliminar is not None and custo_envio_completo else None)

        # Taxas de processamento: estimativas por modalidade, sem gravar como taxas reais.
        taxas_por_pedido = taxas_pedidos(pedidos_fin, chaves_filtradas)
        st.markdown("### Histórico de custos por pedido")
        st.caption("O custo unitário é congelado na primeira consulta. Para pedidos anteriores à implantação, a primeira captura usa o custo atual do catálogo e precisa de validação histórica. O armazenamento local do Streamlit Cloud não é permanente: mantenha o CSV de backup.")
        st.download_button("Baixar backup do histórico de CPV (CSV)",
                           data=exportar_historico_cpv_maHUT(),
                           file_name="MAHUT_historico_cpv.csv", mime="text/csv",
                           key="baixar_historico_cpv_maHUT")
        arquivo_cpv = st.file_uploader("Restaurar histórico de CPV de backup", type=["csv"],
                                       key="arquivo_historico_cpv_maHUT")
        if arquivo_cpv is not None and st.button("Importar histórico de CPV", key="importar_historico_cpv_maHUT"):
            try:
                quantidade_importada = importar_historico_cpv_maHUT(arquivo_cpv)
                st.success(f"Histórico restaurado: {quantidade_importada} registro(s).")
                st.rerun()
            except (ValueError, KeyError, TypeError) as erro:
                st.error(f"Não foi possível importar o histórico: {erro}")

        st.markdown("### Conciliação das taxas do Nuvem Pago")
        st.caption("Taxas reais conferidas por comprovante; outras são estimadas até conciliação. Os lançamentos adicionais permanecem apenas na sessão: exporte o CSV para backup.")
        if "taxas_reais_maHUT" not in st.session_state:
            st.session_state["taxas_reais_maHUT"] = {
                numero: valor_br_sem_simbolo(valor)
                for numero, valor in TAXAS_REAIS_CONFERIDAS.items()
            }
        taxas_guardadas = st.session_state["taxas_reais_maHUT"]
        arquivo_taxas = st.file_uploader("Recuperar taxas reais de um CSV anterior", type=["csv"], key="arquivo_taxas_maHUT")
        if arquivo_taxas is not None and st.button("Importar taxas do CSV", key="importar_taxas_maHUT"):
            try:
                importadas = pd.read_csv(arquivo_taxas, sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig")
                if not {"Pedido", "Taxa real (R$)"}.issubset(importadas.columns):
                    st.error("O CSV deve ter as colunas Pedido e Taxa real (R$).")
                else:
                    invalidas = 0
                    for _, linha in importadas.iterrows():
                        numero = str(linha["Pedido"]).strip()
                        texto = str(linha["Taxa real (R$)"]).strip()
                        if numero and texto:
                            valor = decimal_br(texto)
                            if valor is None:
                                invalidas += 1
                            else:
                                taxas_guardadas[numero] = valor_br_sem_simbolo(valor)
                    st.session_state.pop("editor_taxas_maHUT", None)
                    if invalidas:
                        st.warning(f"{invalidas} taxa(s) inválida(s) foram ignoradas.")
                    else:
                        st.success("Taxas importadas. Confira a tabela abaixo.")
                    st.rerun()
            except Exception:
                st.error("Não foi possível ler o CSV. Use o arquivo exportado pelo próprio BI.")

        registros_editor = []
        for numero, registro in sorted(taxas_por_pedido.items(), key=lambda par: par[0], reverse=True):
            registros_editor.append({
                "Pedido": numero,
                "Meio de pagamento": registro["metodo"],
                "Taxa estimada": moeda(registro["taxa"]) if registro["taxa"] is not None else "Indisponível",
                "Taxa real (R$)": taxas_guardadas.get(numero, ""),
            })
        if registros_editor:
            editor = st.data_editor(
                pd.DataFrame(registros_editor),
                hide_index=True, use_container_width=True, key="editor_taxas_maHUT",
                disabled=["Pedido", "Meio de pagamento", "Taxa estimada"],
                column_config={
                    "Pedido": st.column_config.TextColumn("Pedido"),
                    "Taxa real (R$)": st.column_config.TextColumn("Taxa real (R$) — use vírgula", help="Exemplo: 1,25. Deixe vazio quando ainda não conferiu o extrato."),
                },
            )
            invalidos = []
            for _, linha in editor.iterrows():
                numero = str(linha["Pedido"])
                texto = str(linha["Taxa real (R$)"] or "").strip()
                valor = decimal_br(texto) if texto else None
                if texto and valor is None:
                    invalidos.append(numero)
                    continue
                taxas_guardadas[numero] = valor_br_sem_simbolo(valor)
                if valor is not None:
                    taxas_por_pedido[numero]["taxa"] = valor
                    taxas_por_pedido[numero]["origem"] = "Real — comprovante"
            if invalidos:
                st.warning("Valores inválidos nos pedidos: " + ", ".join(invalidos) + ". Use vírgula decimal e valores não negativos.")
            csv_linhas = [
                {"Pedido": numero, "Taxa real (R$)": valor}
                for numero, valor in sorted(taxas_guardadas.items()) if valor.strip()
            ]
            csv_taxas = pd.DataFrame(csv_linhas, columns=["Pedido", "Taxa real (R$)"]).to_csv(index=False, sep=";", decimal=",")
            st.download_button("Baixar taxas reais em CSV", data="\ufeff" + csv_taxas,
                               file_name="MAHUT_taxas_reais_nuvem_pago.csv",
                               mime="text/csv", key="baixar_taxas_maHUT")
            confirmadas = sum(1 for r in taxas_por_pedido.values() if r["origem"] == "Real — comprovante")
            st.caption(f"{confirmadas} de {len(taxas_por_pedido)} pedido(s) com taxa real conferida neste período.")
        else:
            st.info("Não há pedidos pagos para conciliar neste período.")

        taxas_completas = len(taxas_por_pedido) == len(pagos_fin) and all(
            r["taxa"] is not None for r in taxas_por_pedido.values())
        total_taxas_estimadas = sum(
            (r["taxa"] for r in taxas_por_pedido.values() if r["taxa"] is not None), Decimal("0"))
        resultado_apos_taxas = (
            resultado_com_envio - total_taxas_estimadas
            if resultado_com_envio is not None and taxas_completas else None)

        st.markdown("### Indicadores financeiros preliminares")
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.metric("Faturamento pago", moeda(receita_fin))
        with c2: st.metric("Imposto estimado (4%)", moeda(imposto_fin))
        with c3: st.metric("Após imposto estimado", moeda(apos_imposto_fin))
        with c4: st.metric("Pedidos pagos", str(len(pagos_fin)))
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("Frete cobrado dos clientes", moeda(frete_fin) if not sem_frete_fin else "Incompleto")
        with c2: st.metric("Vendas sem frete cobrado", moeda(receita_fin - frete_fin) if not sem_frete_fin else "Indisponível")
        with c3: st.metric("Lucro líquido", "Ainda não apurado")

        c_cpv, c_res, c_margem = st.columns(3)
        with c_cpv: st.metric("CPV — custos atuais", moeda(cpv_total) if cpv_disponivel else "Incompleto")
        with c_res: st.metric("Após CPV e imposto estimado", moeda(resultado_preliminar) if resultado_preliminar is not None else "Indisponível")
        with c_margem:
            margem = (resultado_preliminar / receita_fin * 100) if resultado_preliminar is not None and receita_fin else None
            st.metric("Margem preliminar", f"{margem:.1f}%".replace(".", ",") if margem is not None else "Indisponível")
        c_envio, c_liq, c_margem_envio = st.columns(3)
        with c_envio: st.metric("Custo de envio informado pela API", moeda(custo_envio_total) if custo_envio_completo else "Incompleto")
        with c_liq: st.metric("Após CPV, imposto e envio informado", moeda(resultado_com_envio) if resultado_com_envio is not None else "Indisponível")
        with c_margem_envio:
            margem_envio = resultado_com_envio / receita_fin * 100 if resultado_com_envio is not None and receita_fin else None
            st.metric("Margem preliminar após envio", f"{margem_envio:.2f}%".replace(".", ",") if margem_envio is not None else "Indisponível")
        st.markdown("### Taxas de pagamento — reais e estimadas")
        st.caption("Para pedidos sem taxa real: Pix estimado em 0,99%; cartão à vista estimado em 5,67% + R$ 0,36. Essas regras não são tarifas oficiais confirmadas.")
        t1, t2, t3 = st.columns(3)
        with t1: st.metric("Taxas reais e estimadas", moeda(total_taxas_estimadas) if taxas_completas else "Incompleto")
        with t2: st.metric("Após CPV, imposto, envio e taxas", moeda(resultado_apos_taxas) if resultado_apos_taxas is not None else "Indisponível")
        with t3:
            margem_taxas = (resultado_apos_taxas / receita_fin * 100) if resultado_apos_taxas is not None and receita_fin else None
            st.metric("Margem preliminar após taxas", f"{margem_taxas:.2f}%".replace(".", ",") if margem_taxas is not None else "Indisponível")
        st.warning("As taxas reais são conferidas por comprovantes; as demais são estimativas. Não utilizar estes indicadores como lucro líquido ou DRE concluída.")
        st.caption("O campo shipping_cost_owner é custo de envio informado pela API, NÃO comprovante do valor pago pela etiqueta. CPV usa custo ATUAL, não histórico. Imposto estimado em 4% do total, inclusive frete (base a validar com contador). Taxas parcialmente conciliadas; não é lucro líquido.")
        if pendencias_cpv:
            st.warning(f"{len(pendencias_cpv)} item(ns) sem conciliação de custo. O CPV e a margem consolidados ficam indisponíveis para evitar valores incorretos.")
            tabela_br(pd.DataFrame(pendencias_cpv), use_container_width=True, hide_index=True)
        elif not linhas_cpv:
            st.info("Sem pedidos pagos com itens para calcular CPV no período.")
        if sem_valor_fin:
            st.warning(f"Existem {sem_valor_fin} pedido(s) pago(s) sem valor total válido. Os totais podem estar incompletos.")
        if sem_frete_fin:
            st.warning(f"Existem {sem_frete_fin} pedido(s) pago(s) sem informação de frete.")

        st.markdown("### Detalhamento financeiro dos pedidos pagos")
        if pagos_fin.empty:
            st.info("Nenhum pedido pago encontrado no período.")
        else:
            detalhes_fin = pagos_fin[["Pedido", "Data", "Valor do pedido (R$)",
                                      "Frete cobrado (R$)", "Desconto informado (R$)"]].copy()
            detalhes_fin["Imposto estimado (R$)"] = detalhes_fin["Valor do pedido (R$)"].apply(
                lambda v: round(v * 0.04, 2) if pd.notna(v) else None)
            detalhes_fin["Após imposto estimado (R$)"] = (
                detalhes_fin["Valor do pedido (R$)"] - detalhes_fin["Imposto estimado (R$)"])
            cpv_por_pedido = {r["Pedido"]: float(r["CPV"]) if r["CPV"] is not None else None for r in linhas_cpv}
            detalhes_fin["CPV — custo atual (R$)"] = detalhes_fin["Pedido"].astype(str).map(cpv_por_pedido)
            detalhes_fin["Após CPV e imposto (R$)"] = (detalhes_fin["Após imposto estimado (R$)"] - detalhes_fin["CPV — custo atual (R$)"])
            detalhes_fin["Custo envio informado (R$)"] = detalhes_fin["Pedido"].astype(str).map(
                {k: float(v) if v is not None else None for k, v in custo_envio_por_pedido.items()})
            detalhes_fin["Após CPV, imposto e envio (R$)"] = (
                detalhes_fin["Após CPV e imposto (R$)"] - detalhes_fin["Custo envio informado (R$)"])
            detalhes_fin["Meio de pagamento"] = detalhes_fin["Pedido"].astype(str).map(
                {k: v["metodo"] for k, v in taxas_por_pedido.items()})
            detalhes_fin["Taxa de pagamento (R$)"] = detalhes_fin["Pedido"].astype(str).map(
                {k: float(v["taxa"]) if v["taxa"] is not None else None for k, v in taxas_por_pedido.items()})
            detalhes_fin["Origem da taxa"] = detalhes_fin["Pedido"].astype(str).map(
                {k: v["origem"] for k, v in taxas_por_pedido.items()})
            detalhes_fin["Após taxas (R$)"] = (
                detalhes_fin["Após CPV, imposto e envio (R$)"] - detalhes_fin["Taxa de pagamento (R$)"])
            detalhes_fin = detalhes_fin.sort_values("Data", ascending=False, na_position="last")
            # Somente a cópia para exibição é convertida em texto no padrão brasileiro.
            exibicao_fin = detalhes_fin.copy()
            for col in exibicao_fin.columns:
                if col.endswith("(R$)"):
                    exibicao_fin[col] = exibicao_fin[col].map(
                        lambda v: moeda(v) if pd.notna(v) else "Pendente")
            tabela_br(exibicao_fin, use_container_width=True, hide_index=True,
                         column_config={"Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY")})
        # =========================================================
        # MÓDULO 5 — PAINEL EXECUTIVO, EVOLUÇÃO E AUDITORIA
        # Apenas leitura; não modifica integrações ou módulos anteriores.
        # =========================================================
        st.markdown("### Evolução financeira — pedidos pagos")
        st.caption("Agrupamento pela data de criação do pedido, não pela data de recebimento bancário. Valores de pedidos já incluem descontos aplicados; não subtraí-los novamente.")
        if not pagos_fin.empty:
            serie_fin = pagos_fin.loc[pagos_fin["Data"].notna(),
                                      ["Data", "Pedido", "_total", "_frete"]].copy()
            serie_fin["Receita paga (R$)"] = serie_fin["_total"].map(
                lambda v: float(v) if v is not None else None)
            serie_fin = serie_fin.dropna(subset=["Receita paga (R$)"])
            if not serie_fin.empty:
                evolucao_fin = (serie_fin.groupby("Data", as_index=False)
                                .agg(**{"Receita paga (R$)": ("Receita paga (R$)", "sum"),
                                        "Pedidos pagos": ("Pedido", "nunique")}))
                evolucao_fin["Data"] = pd.to_datetime(evolucao_fin["Data"])
                # Reindexa dias sem vendas para não ligar pontos distantes como se
                # houvesse faturamento contínuo. Valores diários são pedidos pagos.
                datas_completas = pd.date_range(evolucao_fin["Data"].min(),
                                                evolucao_fin["Data"].max(), freq="D")
                evolucao_fin = (evolucao_fin.set_index("Data")
                                .reindex(datas_completas, fill_value=0)
                                .rename_axis("Data").reset_index())
                grafico_fin = (alt.Chart(evolucao_fin)
                    .mark_bar(color="#A88942", cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
                    .encode(x=alt.X("Data:T", title="Data", axis=alt.Axis(format="%d/%m")),
                            y=alt.Y("Receita paga (R$):Q", title="Faturamento pago (R$)"),
                            tooltip=[alt.Tooltip("Data:T", title="Data", format="%d/%m/%Y"),
                                     alt.Tooltip("Receita paga (R$):Q", title="Receita", format=".2f"),
                                     alt.Tooltip("Pedidos pagos:Q", title="Pedidos")])
                    .properties(height=320))
                st.altair_chart(grafico_fin, use_container_width=True)
                with st.expander("Consultar evolução diária"):
                    tabela_br(evolucao_fin, hide_index=True, use_container_width=True)
            else:
                st.info("Não há totais válidos para exibir a evolução.")
        else:
            st.info("Sem pedidos pagos para o gráfico neste período.")

        st.markdown("### Conferência e qualidade dos dados financeiros")
        qtd_pagos_fin = len(pagos_fin)
        qtd_cpv_ok = sum(1 for r in linhas_cpv if r["Completo"])
        qtd_taxas_reais = sum(1 for r in taxas_por_pedido.values()
                              if r["origem"] == "Real — comprovante")
        qtd_envio_ok = sum(1 for numero in chaves_filtradas
                            if custo_envio_por_pedido.get(numero) is not None)
        c1, c2, c3, c4 = st.columns(4)
        with c1: st.metric("Pedidos com CPV completo", f"{qtd_cpv_ok}/{qtd_pagos_fin}")
        with c2: st.metric("Taxas reais conferidas", f"{qtd_taxas_reais}/{qtd_pagos_fin}")
        with c3: st.metric("Envios com custo informado", f"{qtd_envio_ok}/{qtd_pagos_fin}")
        with c4: st.metric("Pedidos sem total válido", str(sem_valor_fin))
        if qtd_pagos_fin and (qtd_cpv_ok < qtd_pagos_fin or
                             qtd_taxas_reais < qtd_pagos_fin or
                             qtd_envio_ok < qtd_pagos_fin or sem_valor_fin):
            st.warning("Conciliação incompleta: resultados financeiros são gerenciais e provisórios.")
        st.caption("Custo de envio informado não significa valor liquidado; CPV completo não significa custo histórico validado.")

        st.markdown("### Exportação e conferência")
        if not pagos_fin.empty:
            arquivo_detalhes = detalhes_fin.copy()
            arquivo_detalhes["Data"] = arquivo_detalhes["Data"].map(
                lambda v: v.strftime("%d/%m/%Y") if pd.notna(v) else "")
            st.download_button(
                "Baixar detalhamento financeiro em CSV",
                data="\ufeff" + arquivo_detalhes.to_csv(index=False, sep=";", decimal=","),
                file_name="MAHUT_modulo5_detalhamento_financeiro.csv",
                mime="text/csv", key="exportacao_modulo5_detalhes")
        if pendencias_cpv:
            st.download_button(
                "Baixar pendências de custo em CSV",
                data="\ufeff" + pd.DataFrame(pendencias_cpv).to_csv(index=False, sep=";"),
                file_name="MAHUT_modulo5_pendencias_cpv.csv",
                mime="text/csv", key="exportacao_modulo5_pendencias")
        st.caption("Exportações usam o período selecionado e não alteram os dados na Nuvemshop.")

        st.markdown("### Integrações financeiras pendentes")
        st.write("**Taxas de pagamento:** estimativas automáticas disponíveis para Pix e cartão à vista; ainda falta conciliar as taxas efetivamente descontadas por pedido.")
        st.write("**Custos históricos:** registrar custo na data da venda; por enquanto o CPV usa o custo atual lido da Nuvemshop.")
        st.write("**Conciliação das etiquetas:** confirmar que shipping_cost_owner corresponde ao débito efetivo da etiqueta em cada pedido.")
        st.caption("Os filtros seguem a data de criação dos pedidos, como no Módulo 2. Reembolsos parciais precisam de conciliação antes de entrarem no resultado financeiro.")
    except Exception as erro:
        st.error("Não foi possível carregar os dados financeiros da loja virtual.")
        st.caption(f"Detalhe técnico: {type(erro).__name__}")

elif pagina == "6 · Marketing":
    st.markdown("## Marketing")
    st.markdown('<div class="info-box">Indicadores de <b>sessões e aquisição</b> obtidos do GA4. Compras, taxa de conversão e mídia paga ainda aguardam validação e integração.</div>', unsafe_allow_html=True)
    ok, resultado = testar_conexao_ga4()
    c1, c2, c3 = st.columns(3)
    with c1: card("Sessões GA4 — 7 dias completos", str(resultado) if ok else "Indisponível")
    with c2: card("Conversão", "A conectar")
    with c3: card("Mídia paga", "A conectar")
    st.caption("Fonte: Google Analytics 4 · últimos 7 dias completos, excluindo hoje. Atualização em cache por até 15 minutos.")

    if ok:
        try:
            canais = consultar_ga4_canais()
            if canais.empty:
                st.info("GA4 não retornou sessões por canal neste período.")
            else:
                st.markdown('<div class="section">Origem das sessões por canal</div>', unsafe_allow_html=True)
                total_canais = int(canais["Sessões"].sum())
                canais_exibicao = canais.copy()
                canais_exibicao["Participação (%)"] = (
                    canais_exibicao["Sessões"] / total_canais * 100
                ).round(1) if total_canais else 0.0
                tabela_br(
                    canais_exibicao, use_container_width=True, hide_index=True,
                    column_config={"Participação (%)": st.column_config.NumberColumn("Participação (%)", format="%.1f%%")}
                )
                st.bar_chart(canais.set_index("Canal")[["Sessões"]], use_container_width=True)
                st.caption(f"Total por canal: {total_canais} sessões. Os canais seguem a classificação padrão do GA4; Organic Social não significa necessariamente tráfego pago.")
                if total_canais != resultado:
                    st.warning("A soma por canal difere do total de sessões. Confira o período e a classificação no GA4.")
        except Exception:
            st.warning("Não foi possível carregar a divisão por canais do GA4 neste momento.")

        try:
            diario = consultar_ga4_evolucao_diaria()
            st.markdown('<div class="section">Evolução diária das sessões</div>', unsafe_allow_html=True)
            if diario.empty:
                st.info("Nenhuma sessão diária encontrada no período.")
            else:
                st.line_chart(diario.set_index("Data")[["Sessões"]], use_container_width=True)
                st.caption("Evolução das sessões diárias dos últimos 7 dias completos.")
        except Exception:
            st.warning("Não foi possível carregar a evolução diária do GA4 neste momento.")
    else:
        st.warning("Não foi possível consultar as sessões GA4. Verifique a conexão nas Configurações.")

elif pagina == "7 · Influenciadores":
    st.markdown("## Influenciadores — resultados reais")
    st.caption("Vendas pagas da Nuvemshop identificadas pelo cupom. Comissão padrão de 10% sobre mercadorias após descontos, sem frete.")
    try:
        parceiros = listar_parceiros_maHUT()
        envios = listar_envios_maHUT()
    except Exception:
        st.error("Não foi possível carregar o cadastro de influenciadores. Execute primeiro o SQL do Módulo 7 no Supabase e verifique a conexão.")
        st.stop()

    st.caption("O BI cria uma senha provisória individual no Supabase Auth. Não envia convite por e-mail.")
    convites_autorizados = autorizar_convites_maHUT()

    with st.expander("Cadastrar ou atualizar influenciador", expanded=not bool(parceiros)):
        with st.form("form_parceiro_maHUT", clear_on_submit=True):
            nome = st.text_input("Nome do influenciador")
            email = st.text_input("E-mail de acesso ao Portal MAHUT")
            cupom = st.text_input("Cupom exclusivo (sem espaços)")
            comissao_pct = st.number_input("Comissão (%)", min_value=0.0, max_value=100.0, value=10.0, step=0.5)
            if st.form_submit_button("Salvar influenciador"):
                try:
                    salvar_parceiro_maHUT(nome, email, cupom, comissao_pct)
                    st.success("Cadastro comercial salvo. Para criar a senha, use a seção abaixo.")
                    st.rerun()
                except Exception:
                    st.error("Não foi possível salvar. Confira o e-mail e se ele já está vinculado a outro cupom.")

    if convites_autorizados and parceiros:
        with st.expander("Criar acesso provisório ao Portal", expanded=False):
            opcoes_acesso = {f"{p[1]} · {p[2]}": p for p in parceiros}
            escolhido_acesso = st.selectbox("Influenciador cadastrado", list(opcoes_acesso), key="ma_criar_acesso_escolha")
            selecionado_acesso = opcoes_acesso[escolhido_acesso]
            st.caption("Cria conta nova. Contas existentes não são redefinidas automaticamente.")
            if st.button("Gerar senha provisória individual", key="ma_criar_acesso_btn"):
                try:
                    with banco_influenciadores() as con:
                        with con.cursor() as cur:
                            cur.execute("SELECT email FROM mahut_private.influenciadores_bi WHERE id=%s", (selecionado_acesso[0],))
                            linha = cur.fetchone()
                    if not linha or not linha[0]:
                        raise ValueError("E-mail não localizado no cadastro do influenciador.")
                    senha = criar_acesso_provisorio_maHUT(linha[0])
                    st.session_state["ma_senha_provisoria"] = (linha[0], senha)
                except (ValueError, RuntimeError, requests.RequestException) as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("Falha ao consultar o cadastro ou criar a conta. Verifique o Supabase.")
            credencial = st.session_state.get("ma_senha_provisoria")
            if credencial:
                st.success("Conta criada. Copie a senha agora; ela não será armazenada no BI.")
                st.code(f"E-mail: {credencial[0]}\nSenha provisória: {credencial[1]}", language="text")
                st.warning("Entregue a senha ao influenciador por canal privado. Não compartilhe em grupos.")
                if st.button("Ocultar senha provisória", key="ma_ocultar_senha"):
                    st.session_state.pop("ma_senha_provisoria", None)
                    st.rerun()

    if parceiros:
        with st.expander("Gerenciar influenciadores — ativar, desativar ou excluir", expanded=False):
            try:
                estados = listar_status_parceiros_maHUT()
                nomes_gestao = {f"{p[1]} · {p[2]}": p for p in parceiros}
                selecionado_gestao = st.selectbox("Influenciador cadastrado", list(nomes_gestao), key="inf_gestao_selecao")
                parceiro_gestao = nomes_gestao[selecionado_gestao]
                ativo_atual = estados.get(parceiro_gestao[0], True)
                st.write(f"Status atual: **{'Ativo' if ativo_atual else 'Inativo'}**")
                if st.button("Desativar influenciador" if ativo_atual else "Reativar influenciador", key="inf_alterar_status"):
                    atualizar_status_parceiro_maHUT(parceiro_gestao[0], not ativo_atual)
                    st.success("Status atualizado. O bloqueio no portal dependerá da integração de autorização ainda pendente.")
                    st.rerun()
                st.caption("Desativar preserva vendas, comissões e envios. A consulta de resultados históricos continua disponível ao administrador.")
                st.divider()
                st.warning("Exclusão definitiva somente para cadastro criado por engano e sem qualquer venda histórica ou perfume enviado. O BI não consegue comprovar sozinho a inexistência de vendas antigas na Nuvemshop.")
                confirmacao = st.checkbox("Verifiquei todo o histórico da Nuvemshop e confirmo que este cupom nunca teve vendas e que não há envios vinculados.", key="inf_confirma_historico")
                confirmar_nome = st.text_input("Digite EXCLUIR para confirmar", key="inf_confirma_palavra")
                if st.button("Excluir cadastro definitivamente", key="inf_excluir_cadastro", disabled=not (confirmacao and confirmar_nome == "EXCLUIR")):
                    try:
                        excluir_parceiro_sem_movimentacao_maHUT(parceiro_gestao[0])
                        st.success("Cadastro excluído. A conta Supabase Auth, se existir, não foi removida.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
            except Exception:
                st.error("Não foi possível gerenciar status. Execute primeiro a migração SQL de status no Supabase.")

    if not parceiros:
        st.info("Cadastre o primeiro influenciador e seu cupom para iniciar o acompanhamento.")
    else:
        opcoes = {f"{p[1]} · {p[2]}": p[0] for p in parceiros}
        with st.expander("Registrar perfumes enviados", expanded=False):
            with st.form("form_envio_maHUT", clear_on_submit=True):
                escolhido = st.selectbox("Influenciador", list(opcoes))
                dia_envio = st.date_input("Data do envio", value=datetime.now().date(), format="DD/MM/YYYY")
                perfume = st.text_input("Perfume e tamanho")
                sku = st.text_input("SKU (opcional)")
                quantidade = st.number_input("Quantidade", min_value=1, value=1, step=1)
                custo = st.number_input("Custo unitário (R$)", min_value=0.0, value=0.0, step=0.5)
                if st.form_submit_button("Registrar envio"):
                    try:
                        salvar_envio_maHUT(opcoes[escolhido], dia_envio, perfume, sku, int(quantidade), custo)
                        st.success("Envio registrado no Supabase.")
                        st.rerun()
                    except Exception:
                        st.error("Não foi possível registrar o envio.")

        hoje = datetime.now().date()
        c_periodo1, c_periodo2 = st.columns(2)
        with c_periodo1:
            inicio_inf = st.date_input("De", value=hoje.replace(day=1), format="DD/MM/YYYY", key="inf_inicio")
        with c_periodo2:
            fim_inf = st.date_input("Até", value=hoje, format="DD/MM/YYYY", key="inf_fim")
        if inicio_inf > fim_inf:
            st.warning("A data inicial deve ser anterior à final.")
            st.stop()
        if st.button("Atualizar pedidos da Nuvemshop", key="atualizar_inf"):
            consultar_pedidos_nuvemshop.clear()
            st.rerun()
        try:
            pedidos_inf = consultar_pedidos_nuvemshop()
            vendas, ambiguos, sem_base = vendas_influenciadores_maHUT(pedidos_inf, parceiros, inicio_inf, fim_inf)
        except Exception:
            st.error("Não foi possível consultar os pedidos da Nuvemshop neste momento.")
            st.stop()

        resumo = []
        for p in parceiros:
            id_parceiro, nome, cupom, pct = p
            vendas_p = [v for v in vendas if v["ID parceiro"] == id_parceiro]
            envios_p = [e for e in envios if e[1] == id_parceiro and inicio_inf <= e[3] <= fim_inf]
            capital = sum((Decimal(str(e[7])) * Decimal(str(e[6])) for e in envios_p), Decimal("0"))
            faturamento = sum((v["Faturamento"] for v in vendas_p), Decimal("0"))
            comissao = sum((v["Comissão"] for v in vendas_p), Decimal("0"))
            investimento = capital + comissao
            resumo.append({"Influenciador": nome, "Cupom": cupom, "Pedidos": len(vendas_p),
                           "Faturamento": faturamento, "Comissão": comissao,
                           "Capital alocado": capital, "Investimento total": investimento,
                           "Retorno": faturamento / investimento if investimento > 0 else None})

        c1, c2, c3, c4 = st.columns(4)
        with c1: card("Pedidos atribuídos", str(len(vendas)))
        with c2: card("Faturamento atribuído", moeda(float(sum((r["Faturamento"] for r in resumo), Decimal("0")))))
        with c3: card("Capital alocado", moeda(float(sum((r["Capital alocado"] for r in resumo), Decimal("0")))))
        with c4: card("Comissão estimada", moeda(float(sum((r["Comissão"] for r in resumo), Decimal("0")))))

        st.markdown("### Desempenho por influenciador")
        tabela = []
        for r in resumo:
            tabela.append({"Influenciador": r["Influenciador"], "Cupom": r["Cupom"],
                           "Pedidos": r["Pedidos"], "Faturamento": moeda(float(r["Faturamento"])),
                           "Comissão": moeda(float(r["Comissão"])),
                           "Capital alocado": moeda(float(r["Capital alocado"])),
                           "Investimento total": moeda(float(r["Investimento total"])),
                           "Retorno": (f'{r["Retorno"]:.2f}x'.replace(".", ",") if r["Retorno"] is not None else "—")})
        st.dataframe(pd.DataFrame(tabela), use_container_width=True, hide_index=True)
        st.caption("Retorno = faturamento de mercadorias atribuído ÷ (capital alocado + comissão). Não representa lucro líquido.")
        if ambiguos:
            st.warning(f"{len(ambiguos)} pedido(s) com mais de um cupom cadastrado: não atribuídos para evitar duplicidade.")
        if sem_base:
            st.warning(f"{len(sem_base)} pedido(s) com valores incompletos: não incluídos nos indicadores.")
        st.markdown("### Pedidos atribuídos")
        if vendas:
            st.dataframe(pd.DataFrame([{"Pedido": v["Pedido"], "Data": v["Data"].strftime("%d/%m/%Y"),
                "Influenciador": v["Influenciador"], "Cupom": v["Cupom"],
                "Faturamento": moeda(float(v["Faturamento"])), "Comissão": moeda(float(v["Comissão"]))}
                for v in vendas]), use_container_width=True, hide_index=True)
        else:
            st.info("Nenhum pedido pago identificado pelos cupons cadastrados no período.")
        st.markdown("### Perfumes enviados")
        envios_periodo = [e for e in envios if inicio_inf <= e[3] <= fim_inf]
        if envios_periodo:
            st.dataframe(pd.DataFrame([{"Data": e[3].strftime("%d/%m/%Y"), "Influenciador": e[2],
                "Perfume": e[4], "SKU": e[5], "Quantidade": e[6],
                "Custo unitário": moeda(float(e[7])), "Capital": moeda(float(e[7]*e[6]))}
                for e in envios_periodo]), use_container_width=True, hide_index=True)
            with st.expander("Excluir um registro de envio"):
                ids = {f"{e[3].strftime('%d/%m/%Y')} · {e[2]} · {e[4]} · {e[6]} un. · ID {e[0]}": e[0] for e in envios_periodo}
                remover = st.selectbox("Registro", list(ids), key="inf_remover_envio")
                confirmar = st.checkbox("Confirmo a exclusão deste envio", key="inf_confirma_exclusao")
                if st.button("Excluir envio selecionado", disabled=not confirmar):
                    try:
                        remover_envio_maHUT(ids[remover])
                        st.success("Registro excluído.")
                        st.rerun()
                    except Exception:
                        st.error("Não foi possível excluir o registro.")
        else:
            st.info("Nenhum perfume enviado foi registrado neste período.")
        st.caption("Acesso individual dos influenciadores será implementado separadamente; este painel é administrativo.")

elif pagina == "8 · Metas & Indicadores":
    aviso_demo()
    st.markdown("## Metas & Indicadores")
    meta = 10000.00
    realizado = faturamento_demo
    percentual = realizado / meta if meta > 0 else 0
    c1, c2, c3 = st.columns(3)
    with c1: card("Meta mensal", moeda(meta))
    with c2: card("Realizado demonstrativo", moeda(realizado))
    with c3: card("Atingimento demonstrativo", f"{percentual * 100:.1f}%".replace(".", ","))
    st.markdown('<div class="section">Progresso da meta</div>', unsafe_allow_html=True)
    st.progress(min(percentual, 1.0))
    st.caption("Na versão conectada, a meta será editável e comparada com realizado e projeção.")


elif pagina == "9 · DRE Gerencial":
    st.markdown("## DRE Gerencial — MAHUT")
    st.caption("Pedidos pagos da Nuvemshop e receitas manuais não duplicadas. Transferências entre contas não são receitas.")
    st.warning("DRE em construção: usa a data de criação dos pedidos, custos atuais dos produtos, imposto estimado e custo de envio informado pela API. Não substitui a DRE contábil.")
    hoje_dre = datetime.now().date()
    mes_inicio = hoje_dre.replace(day=1)
    escolha_dre = st.selectbox(
        "Período da DRE",
        ["Este mês", "Mês anterior", "Este ano", "Todos os pedidos", "Período personalizado"],
        key="periodo_dre"
    )
    if escolha_dre == "Este mês":
        inicio_dre, fim_dre = mes_inicio, hoje_dre
    elif escolha_dre == "Mês anterior":
        fim_dre = mes_inicio - timedelta(days=1)
        inicio_dre = fim_dre.replace(day=1)
    elif escolha_dre == "Este ano":
        inicio_dre, fim_dre = hoje_dre.replace(month=1, day=1), hoje_dre
    elif escolha_dre == "Todos os pedidos":
        inicio_dre, fim_dre = datetime(2000, 1, 1).date(), hoje_dre
    else:
        datas_dre = st.date_input(
            "Data inicial e final", value=(mes_inicio, hoje_dre),
            format="DD/MM/YYYY", key="datas_dre"
        )
        if isinstance(datas_dre, (tuple, list)) and len(datas_dre) == 2:
            inicio_dre, fim_dre = datas_dre
        else:
            inicio_dre, fim_dre = hoje_dre, hoje_dre - timedelta(days=1)
            st.info("Escolha as duas datas para visualizar o período.")

    if st.button("Atualizar fontes da DRE", key="atualizar_dre"):
        consultar_pedidos_nuvemshop.clear()
        consultar_produtos_nuvemshop_diagnostico.clear()
        st.rerun()
    try:
        pedidos_dre = filtrar_pedidos_dre(consultar_pedidos_nuvemshop(), inicio_dre, fim_dre)
        catalogo_dre = consultar_produtos_nuvemshop_diagnostico()
        if "taxas_reais_maHUT" not in st.session_state:
            st.session_state["taxas_reais_maHUT"] = {
                n: valor_br_sem_simbolo(v) for n, v in TAXAS_REAIS_CONFERIDAS.items()
            }
        detalhes_dre, pendencias_dre = compor_dre(
            pedidos_dre, catalogo_dre, st.session_state["taxas_reais_maHUT"]
        )
        quantidade_dre = len(detalhes_dre)
        receita_paga = somar_dre(detalhes_dre, "Total")
        descontos_dre = somar_dre(detalhes_dre, "Desconto")
        receita_bruta = (
            receita_paga + descontos_dre
            if receita_paga is not None and descontos_dre is not None else None
        )
        impostos_dre = (
            (receita_paga * Decimal("0.04")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if receita_paga is not None else None
        )
        receita_liquida = subtrair_dre(receita_bruta, descontos_dre, impostos_dre)
        cpv_dre = somar_dre(detalhes_dre, "CPV")
        lucro_bruto = subtrair_dre(receita_liquida, cpv_dre)
        frete_dre = somar_dre(detalhes_dre, "Custo de envio API")
        taxas_dre = somar_dre(detalhes_dre, "Taxa")
        frete_recebido_dre = somar_dre(detalhes_dre, "Frete recebido")

        st.markdown("### Receitas manuais — vendas fora da Nuvemshop")
        st.caption("Cadastre vendas do showroom e outros recebimentos operacionais não incluídos nos pedidos da Nuvemshop. Informe o custo dos produtos vendidos (CPV) nas vendas manuais.")
        st.warning("Armazenamento local provisório: receitas manuais podem ser perdidas após reinicialização do Streamlit Cloud. Exporte o backup CSV após cada alteração.")
        with st.expander("➕ Lançar receita manual", expanded=False):
            with st.form("nova_receita_maHUT", clear_on_submit=True):
                data_receita = st.date_input("Data da receita", value=hoje_dre, format="DD/MM/YYYY", key="data_nova_receita")
                categoria_receita = st.selectbox("Categoria da receita", CATEGORIAS_RECEITAS_MAHUT, key="categoria_nova_receita")
                descricao_receita = st.text_input("Descrição da receita", placeholder="Ex.: Venda de 2 perfumes no showroom", key="descricao_nova_receita")
                valor_receita = st.text_input("Valor recebido (R$)", placeholder="Ex.: 200,00", key="valor_nova_receita")
                custo_receita = st.text_input("CPV — custo dos produtos vendidos (R$)", placeholder="Ex.: 45,00; use 0,00 se não se aplicar", key="custo_nova_receita")
                referencia_receita = st.text_input("Referência interna / comprovante (opcional)", key="referencia_nova_receita")
                confirma_nao_duplicada = st.checkbox("Confirmo que esta receita NÃO está registrada nos pedidos da Nuvemshop", key="confirmar_nao_duplicada_receita")
                confirma_cpv_zero = st.checkbox("Confirmo que o CPV informado é zero ou não se aplica", key="confirmar_cpv_zero_receita")
                salvar_receita = st.form_submit_button("Salvar receita", type="primary")
            if salvar_receita:
                try:
                    valor_validado = decimal_br(valor_receita)
                    custo_validado = decimal_br(custo_receita)
                    if valor_validado is None or valor_validado <= 0 or custo_validado is None:
                        raise ValueError("Informe receita positiva e CPV válido (use 0,00 quando não se aplicar)")
                    if custo_validado == 0 and not confirma_cpv_zero:
                        raise ValueError("Confirme o custo zero antes de salvar")
                    if not confirma_nao_duplicada:
                        raise ValueError("Confirme que a receita não está na Nuvemshop")
                    gravar_receita_maHUT(None, data_receita, categoria_receita, descricao_receita,
                                        valor_validado, custo_validado, referencia_receita)
                    st.session_state["aviso_receitas_maHUT"] = "Receita cadastrada. Exporte um novo backup CSV."
                    st.rerun()
                except (ValueError, sqlite3.Error) as erro:
                    st.error(f"Não foi possível salvar a receita: {erro}")

        registros_receitas = listar_receitas_maHUT(inicio_dre, fim_dre)
        receita_manual_total = sum((Decimal(r["valor_centavos"]) / 100 for r in registros_receitas), Decimal("0"))
        cpv_manual_total = sum((Decimal(r["cpv_centavos"]) / 100 for r in registros_receitas), Decimal("0"))
        # O imposto de 4% é uma estimativa somente para vendas de perfumes,
        # não para a categoria de outras receitas operacionais.
        base_imposto_manual = sum((Decimal(r["valor_centavos"]) / 100 for r in registros_receitas
                                  if r["categoria"] != "Outras receitas operacionais"), Decimal("0"))
        imposto_manual_estimado = (base_imposto_manual * Decimal("0.04")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        c_rec1, c_rec2, c_rec3 = st.columns(3)
        with c_rec1: st.metric("Receitas manuais no período", moeda(receita_manual_total))
        with c_rec2: st.metric("CPV manual no período", moeda(cpv_manual_total))
        with c_rec3: st.metric("Registros manuais", len(registros_receitas))
        if registros_receitas:
            tabela_br(pd.DataFrame([{
                "Data": datetime.strptime(r["data"], "%Y-%m-%d").date(),
                "Categoria": r["categoria"], "Descrição": r["descricao"],
                "Receita (R$)": moeda(Decimal(r["valor_centavos"]) / 100),
                "CPV (R$)": moeda(Decimal(r["cpv_centavos"]) / 100),
                "Referência": r["pedido_referencia"]
            } for r in registros_receitas]), hide_index=True, use_container_width=True)
            with st.expander("Editar ou excluir uma receita"):
                opcoes_receitas = {f'{r["data"]} | {r["categoria"]} | {r["descricao"]} | {r["id"][:8]}': r for r in registros_receitas}
                selecionada_receita = st.selectbox("Selecione a receita", list(opcoes_receitas), key="selecionar_receita_edicao")
                atual_receita = opcoes_receitas[selecionada_receita]
                with st.form("editar_receita_maHUT"):
                    dia_editado_receita = st.date_input("Data", value=datetime.strptime(atual_receita["data"], "%Y-%m-%d").date(), format="DD/MM/YYYY", key="editar_data_receita")
                    categoria_editada_receita = st.selectbox("Categoria", CATEGORIAS_RECEITAS_MAHUT, index=CATEGORIAS_RECEITAS_MAHUT.index(atual_receita["categoria"]), key="editar_categoria_receita")
                    descricao_editada_receita = st.text_input("Descrição", value=atual_receita["descricao"], key="editar_descricao_receita")
                    valor_editado_receita = st.text_input("Valor (R$)", value=valor_br_sem_simbolo(Decimal(atual_receita["valor_centavos"]) / 100), key="editar_valor_receita")
                    cpv_editado_receita = st.text_input("CPV (R$)", value=valor_br_sem_simbolo(Decimal(atual_receita["cpv_centavos"]) / 100), key="editar_cpv_receita")
                    referencia_editada_receita = st.text_input("Referência", value=atual_receita["pedido_referencia"], key="editar_referencia_receita")
                    confirmar_edicao_receita = st.checkbox("Confirmo que não há duplicidade com a Nuvemshop", key="confirmar_edicao_receita")
                    salvar_edicao_receita = st.form_submit_button("Salvar alterações")
                if salvar_edicao_receita:
                    try:
                        v, c = decimal_br(valor_editado_receita), decimal_br(cpv_editado_receita)
                        if v is None or v <= 0 or c is None or not confirmar_edicao_receita:
                            raise ValueError("Confira os valores e confirme que não há duplicidade")
                        gravar_receita_maHUT(atual_receita["id"], dia_editado_receita, categoria_editada_receita,
                                            descricao_editada_receita, v, c, referencia_editada_receita)
                        st.session_state["aviso_receitas_maHUT"] = "Receita atualizada. Exporte um novo backup CSV."
                        st.rerun()
                    except (ValueError, sqlite3.Error) as erro:
                        st.error(str(erro))
                confirmar_exclusao_receita = st.checkbox("Confirmo a exclusão desta receita", key="confirmar_exclusao_receita")
                if st.button("Excluir receita", disabled=not confirmar_exclusao_receita, key="excluir_receita_maHUT"):
                    excluir_receita_maHUT(atual_receita["id"])
                    st.session_state["aviso_receitas_maHUT"] = "Receita excluída. Exporte um novo backup CSV."
                    st.rerun()
        else:
            st.info("Nenhuma receita manual registrada no período selecionado.")
        if "aviso_receitas_maHUT" in st.session_state:
            st.info(st.session_state.pop("aviso_receitas_maHUT"))
        st.markdown("#### Backup e restauração das receitas manuais")
        st.caption(f"Receitas salvas neste servidor, em todos os períodos: {len(listar_receitas_maHUT())}.")
        col_export_rec, col_import_rec = st.columns(2)
        with col_export_rec:
            st.download_button("⬇️ Exportar receitas (CSV)", exportar_receitas_maHUT(),
                               file_name="MAHUT_Receitas_Backup.csv", mime="text/csv", key="backup_receitas_maHUT")
        with col_import_rec:
            with st.expander("⬆️ Restaurar receitas de CSV"):
                arquivo_receitas = st.file_uploader("Arquivo de backup de receitas", type=["csv"], key="arquivo_backup_receitas")
                confirmar_backup_receitas = st.checkbox("Confirmo a restauração das receitas", key="confirmar_backup_receitas")
                if st.button("Restaurar receitas", disabled=arquivo_receitas is None or not confirmar_backup_receitas,
                             key="restaurar_receitas_maHUT"):
                    try:
                        resumo_receitas = importar_receitas_maHUT(arquivo_receitas)
                        st.session_state["resumo_receitas_maHUT"] = resumo_receitas
                        st.rerun()
                    except (ValueError, TypeError, sqlite3.Error, pd.errors.ParserError) as erro:
                        st.error(f"Não foi possível restaurar receitas: {erro}")
        if "resumo_receitas_maHUT" in st.session_state:
            resumo_receitas = st.session_state.pop("resumo_receitas_maHUT")
            st.success(f"Restauração: {resumo_receitas['novos']} novos, {resumo_receitas['atualizados']} atualizados, "
                       f"{resumo_receitas['ja_existentes']} existentes e {resumo_receitas['duplicados_ignorados']} duplicados ignorados.")

        st.markdown("### Despesas comerciais e operacionais")
        st.caption("Cadastre despesas da MAHUT com data, categoria, descrição e valor. O BI considera apenas os lançamentos dentro do período selecionado.")
        st.warning("Armazenamento local provisório: no Streamlit Community Cloud, os dados podem desaparecer após reinicializações ou atualizações do aplicativo. Faça backup pelo botão Baixar despesas em CSV. Para persistência garantida, será necessário conectar um banco externo.")
        with st.expander("➕ Adicionar despesa", expanded=False):
            with st.form("nova_despesa_maHUT", clear_on_submit=True):
                data_nova = st.date_input("Data da despesa", value=hoje_dre, format="DD/MM/YYYY", key="data_nova_despesa")
                categoria_nova = st.selectbox("Categoria", CATEGORIAS_DESPESAS_MAHUT, key="categoria_nova_despesa")
                descricao_nova = st.text_input("Descrição", placeholder="Ex.: Impulsionamento Instagram", key="descricao_nova_despesa")
                valor_novo = st.text_input("Valor (R$)", placeholder="Ex.: 150,00", key="valor_nova_despesa")
                salvar_nova = st.form_submit_button("Salvar despesa", type="primary")
            if salvar_nova:
                try:
                    valor_validado = decimal_br(valor_novo)
                    if valor_validado is None:
                        raise ValueError("Informe um valor válido, como 150,00")
                    gravar_despesa_maHUT(None, data_nova, categoria_nova, descricao_nova, valor_validado)
                    st.session_state["aviso_backup_maHUT"] = "Despesa cadastrada. Baixe um novo backup CSV antes de encerrar."
                    st.rerun()
                except (ValueError, sqlite3.Error) as erro:
                    st.error(f"Não foi possível cadastrar: {erro}")

        registros_despesas = listar_despesas_maHUT(inicio_dre, fim_dre)
        despesa_total = sum((Decimal(d["valor_centavos"]) / 100 for d in registros_despesas), Decimal("0"))
        totais_categoria = {
            categoria: sum((Decimal(d["valor_centavos"]) / 100 for d in registros_despesas if d["categoria"] == categoria), Decimal("0"))
            for categoria in CATEGORIAS_DESPESAS_MAHUT
        }
        st.metric("Despesas manuais no período", moeda(despesa_total))
        if registros_despesas:
            tabela_br(pd.DataFrame([{
                "Data": datetime.strptime(d["data"], "%Y-%m-%d").date(),
                "Categoria": d["categoria"], "Descrição": d["descricao"],
                "Valor": moeda(Decimal(d["valor_centavos"]) / 100)
            } for d in registros_despesas]), hide_index=True, use_container_width=True,
                column_config={"Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY")})
            with st.expander("Editar ou excluir uma despesa"):
                opcoes = {f'{d["data"]} | {d["categoria"]} | {d["descricao"]} | {moeda(Decimal(d["valor_centavos"]) / 100)} | {d["id"][:8]}': d for d in registros_despesas}
                escolhido = st.selectbox("Selecione o lançamento", list(opcoes), key="editar_selecao_despesa")
                atual = opcoes[escolhido]
                with st.form("editar_despesa_maHUT"):
                    dia_editado = st.date_input("Data", value=datetime.strptime(atual["data"], "%Y-%m-%d").date(), format="DD/MM/YYYY")
                    categoria_editada = st.selectbox("Categoria", CATEGORIAS_DESPESAS_MAHUT, index=CATEGORIAS_DESPESAS_MAHUT.index(atual["categoria"]))
                    descricao_editada = st.text_input("Descrição", value=atual["descricao"])
                    valor_editado = st.text_input("Valor (R$)", value=f'{Decimal(atual["valor_centavos"]) / 100:.2f}'.replace(".", ","))
                    salvar_edicao = st.form_submit_button("Salvar alterações")
                if salvar_edicao:
                    try:
                        novo_valor = decimal_br(valor_editado)
                        if novo_valor is None:
                            raise ValueError("Valor inválido")
                        gravar_despesa_maHUT(atual["id"], dia_editado, categoria_editada, descricao_editada, novo_valor)
                        st.session_state["aviso_backup_maHUT"] = "Despesa atualizada. Baixe um novo backup CSV."
                        st.rerun()
                    except (ValueError, sqlite3.Error) as erro:
                        st.error(str(erro))
                confirmar_exclusao = st.checkbox("Confirmo que desejo excluir este lançamento", key="confirmar_exclusao_despesa")
                if st.button("Excluir despesa", disabled=not confirmar_exclusao, key="excluir_despesa"):
                    excluir_despesa_maHUT(atual["id"])
                    st.session_state["aviso_backup_maHUT"] = "Despesa excluída. Substitua seu backup antigo por um novo CSV."
                    st.rerun()
        else:
            st.info("Nenhuma despesa manual registrada no período selecionado.")

        if "aviso_backup_maHUT" in st.session_state:
            st.info(st.session_state.pop("aviso_backup_maHUT"))
        st.markdown("### Segurança dos lançamentos — backup e restauração")
        todos_registros = listar_despesas_maHUT()
        st.caption(f"Total de despesas salvas neste servidor: {len(todos_registros)}. "
                   "O backup inclui todos os períodos, mesmo quando a DRE está filtrada por mês.")
        st.warning("O arquivo SQLite do Streamlit Cloud é temporário. "
                   "Depois de cada cadastro, edição ou exclusão, baixe e guarde um NOVO CSV fora do BI. "
                   "Antes de cadastrar despesas em outra sessão, confira o total de registros e restaure o último backup se necessário.")
        c_backup, c_import = st.columns(2)
        with c_backup:
            st.download_button("⬇️ Exportar backup completo (CSV)", exportar_despesas_maHUT(),
                               file_name="MAHUT_Despesas_Backup.csv", mime="text/csv", key="backup_despesas")
            st.caption("Guarde o arquivo no computador ou no Drive. O BI não salva automaticamente o CSV fora do servidor.")
        with c_import:
            with st.expander("⬆️ Restaurar backup CSV — sem duplicar registros"):
                arquivo_backup = st.file_uploader("Selecione o CSV exportado pelo BI", type=["csv"], key="arquivo_backup_despesas")
                st.caption("O sistema aceita datas DD/MM/AAAA ou AAAA-MM-DD e valida o arquivo antes de gravar. IDs existentes não são duplicados; "
                           "lançamentos iguais também são reconhecidos.")
                confirmar_importacao = st.checkbox("Confirmo a restauração do backup selecionado", key="confirmar_backup_maHUT")
                if st.button("Restaurar despesas", disabled=arquivo_backup is None or not confirmar_importacao,
                             key="importar_backup_despesas"):
                    try:
                        resumo = importar_despesas_maHUT(arquivo_backup)
                        st.session_state["resumo_restauracao_maHUT"] = resumo
                        st.rerun()
                    except (ValueError, TypeError, sqlite3.Error, pd.errors.ParserError) as erro:
                        st.error(f"Não foi possível restaurar o backup: {erro}")
        if "resumo_restauracao_maHUT" in st.session_state:
            resumo = st.session_state.pop("resumo_restauracao_maHUT")
            st.success(f"Backup conferido: {resumo['novos']} novo(s), "
                       f"{resumo['atualizados']} atualizado(s), "
                       f"{resumo['ja_existentes']} já existente(s) e "
                       f"{resumo['duplicados_ignorados']} duplicado(s) ignorado(s).")

        # Receita manual integra a DRE uma única vez; CPV e imposto correspondentes
        # são abatidos separadamente. Não presumir taxas de cartão ou frete nas vendas manuais.
        receita_total_gerencial = receita_paga + receita_manual_total if receita_paga is not None else None
        resultado_operacional = subtrair_dre(lucro_bruto, frete_dre, taxas_dre, despesa_total)
        if resultado_operacional is not None:
            resultado_operacional += receita_manual_total - cpv_manual_total - imposto_manual_estimado

        st.markdown("### Demonstrativo de resultados")
        linhas_dre = [
            exibir_linha_dre("(+) Receita bruta (reconstituída)", receita_bruta, receita_paga, "Valor pago + descontos; inclui frete cobrado ao cliente"),
            exibir_linha_dre("(-) Descontos concedidos", descontos_dre, receita_paga, "Já deduzidos do total pago; exibidos sem dupla subtração"),
            exibir_linha_dre("(-) Impostos estimados (4%)", impostos_dre, receita_paga, "Base tributária ainda precisa de validação contábil"),
            exibir_linha_dre("(=) Receita líquida gerencial", receita_liquida, receita_paga),
            exibir_linha_dre("(-) CPV — custo atual do catálogo", cpv_dre, receita_paga, "Não é custo histórico da data da venda"),
            exibir_linha_dre("(=) Lucro bruto preliminar", lucro_bruto, receita_paga),
            exibir_linha_dre("(-) Custo de envio informado pela API", frete_dre, receita_paga, "Não comprova o valor debitado da etiqueta"),
            exibir_linha_dre("(-) Taxas Nuvem Pago reais/estimadas", taxas_dre, receita_paga),
        ]
        linhas_dre.extend([
            exibir_linha_dre("(+) Receitas manuais fora da Nuvemshop", receita_manual_total, receita_total_gerencial, "Não duplicar pedidos da loja virtual"),
            exibir_linha_dre("(-) CPV das vendas manuais", cpv_manual_total, receita_total_gerencial),
            exibir_linha_dre("(-) Imposto estimado das vendas manuais (4%)", imposto_manual_estimado, receita_total_gerencial,
                            "Estimativa sobre vendas manuais; outras receitas operacionais excluídas da base"),
        ])
        for nome, valor_categoria in totais_categoria.items():
            if valor_categoria > 0:
                linhas_dre.append(exibir_linha_dre("(-) " + nome, valor_categoria, receita_paga, "Despesa manual registrada no período"))
        linhas_dre.append(exibir_linha_dre("(=) Resultado operacional preliminar", resultado_operacional, receita_paga, "Não é lucro líquido contábil"))
        tabela_br(pd.DataFrame(linhas_dre), hide_index=True, use_container_width=True)

        c1, c2, c3, c4 = st.columns(4)
        with c1: st.metric("Pedidos pagos", quantidade_dre)
        with c2: st.metric("Receita total gerencial", moeda(receita_total_gerencial) if receita_total_gerencial is not None else "Pendente")
        with c3: st.metric("Resultado preliminar", moeda(resultado_operacional) if resultado_operacional is not None else "Pendente")
        with c4: st.metric("Margem operacional preliminar", percentual_dre(resultado_operacional, receita_total_gerencial))

        taxas_reais_qtd = sum(r["Origem da taxa"] == "Real — comprovante" for r in detalhes_dre)
        st.caption(f"Taxas reais conferidas: {taxas_reais_qtd} de {quantidade_dre} pedido(s). Frete cobrado dos clientes: {moeda(frete_recebido_dre) if frete_recebido_dre is not None else 'Pendente'} (já incluído na receita paga).")
        if pendencias_dre:
            st.warning(f"CPV incompleto: {len(pendencias_dre)} item(ns) pendente(s). Resultado consolidado não é apresentado.")
        if any(r["Custo de envio API"] is None for r in detalhes_dre):
            st.warning("Existem pedidos sem custo de envio retornado pela API. Resultado consolidado indisponível.")
        if any(r["Taxa"] is None for r in detalhes_dre):
            st.warning("Existem pedidos sem taxa real ou estimativa válida. Resultado consolidado indisponível.")
        if any(r["Origem da taxa"] != "Real — comprovante" for r in detalhes_dre):
            st.info("Algumas taxas ainda são estimadas. Confira os comprovantes no Módulo 5 — Financeiro.")
        st.caption("O valor pago da Nuvemshop já considera descontos e frete cobrado. Receitas manuais são somadas separadamente, com CPV e imposto estimado das vendas manuais. Taxas, frete e outras despesas de vendas manuais não são presumidos. Armazenamento local requer backups CSV e não garante persistência no Streamlit Cloud.")

        if detalhes_dre:
            st.markdown("### Pedidos que compõem a DRE")
            tabela_dre = pd.DataFrame(detalhes_dre)
            for campo in ["Total", "Desconto", "Frete recebido", "Custo de envio API", "CPV", "Taxa"]:
                tabela_dre[campo] = tabela_dre[campo].map(
                    lambda valor: moeda(valor) if valor is not None else "Pendente"
                )
            tabela_br(tabela_dre, hide_index=True, use_container_width=True,
                         column_config={"Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY")})
        st.download_button(
            "Baixar demonstrativo em CSV",
            data="\ufeff" + pd.DataFrame(linhas_dre).to_csv(index=False, sep=";", decimal=","),
            file_name=f"MAHUT_DRE_Gerencial_{inicio_dre}_{fim_dre}.csv",
            mime="text/csv", key="baixar_dre"
        )
    except Exception as erro:
        st.error("Não foi possível montar a DRE. Confira as conexões em Configurações.")
        st.caption(f"Detalhe técnico: {type(erro).__name__}: {str(erro)[:180]}")

elif pagina == "Configurações":
    st.markdown("## Configurações")
    st.number_input("Meta mensal (R$)", min_value=0.0, value=10000.0, step=500.0,
                    help="Valor de referência nesta tela; ainda não está ligado aos indicadores demonstrativos.")
    st.markdown("### Fontes de dados")
    st.selectbox("Vendas", ["Dados demonstrativos", "Nuvemshop"])
    st.selectbox("Estoque", ["Dados demonstrativos", "Bling"])
    st.selectbox("Analytics", ["Dados demonstrativos", "GA4"])
    st.selectbox("Mídia", ["Dados demonstrativos", "Meta"])
    st.info("As seleções acima são uma prévia da estrutura de integrações. Não conectam Nuvemshop, Bling ou Meta automaticamente. A consulta GA4 abaixo já utiliza a integração real.")
    painel_autenticacao_bling()
    st.divider()
    st.subheader("Conexão Supabase — banco seguro")
    st.caption("Teste somente leitura. Não salva nem modifica tokens, pedidos ou históricos.")
    if st.button("Testar conexão Supabase", key="teste_supabase_maHUT"):
        sucesso, mensagem = testar_conexao_supabase_maHUT()
        if sucesso:
            st.success(mensagem)
        else:
            st.error(mensagem)
    st.divider()
    st.subheader("Conexão Nuvemshop")

    if st.button("Testar conexão Nuvemshop"):
        sucesso, mensagem = testar_conexao_nuvemshop()

        if sucesso:
            st.success(mensagem)
        else:
            st.error(mensagem)
    if st.button("Consultar quantidade de pedidos"):
        try:
            pedidos = consultar_pedidos_nuvemshop()
            st.success(f"Consulta concluída: {len(pedidos)} pedidos encontrados.")
        except Exception:
            st.error("Não foi possível consultar os pedidos da Nuvemshop.")
    if st.button("Consultar status dos pagamentos"):
        try:
            pedidos = consultar_pedidos_nuvemshop()

            traducao_status = {
                "paid": "Pago",
                "pending": "Pendente",
                "voided": "Cancelado",
                "authorized": "Autorizado",
                "refunded": "Reembolsado",
                "partially_refunded": "Parcialmente reembolsado"
            }

            status = pd.Series(
                [
                    traducao_status.get(
                        p.get("payment_status"),
                        p.get("payment_status") or "Não informado"
                    )
                    for p in pedidos
                ]
            ).value_counts()

            st.markdown("### Pedidos por status de pagamento")
            tabela_br(
                status.rename_axis("Status").reset_index(name="Quantidade"),
                hide_index=True
            )

        except Exception:
            st.error("Não foi possível consultar os status dos pagamentos.")

    if st.button("Verificar campos financeiros dos pedidos"):
        try:
            pedidos = consultar_pedidos_nuvemshop()

            if pedidos:
                campos = [
                    "total",
                    "subtotal",
                    "discount",
                    "shipping_cost_customer",
                    "payment_status",
                    "status",
                    "created_at",
                    "paid_at"
                ]

                encontrados = [
                    campo for campo in campos
                    if campo in pedidos[0]
                ]

                st.write(
                    "Campos disponíveis:",
                    ", ".join(encontrados)
                )
            else:
                st.info("Nenhum pedido encontrado.")

        except Exception:
            st.error("Não foi possível verificar os campos financeiros.")


    st.subheader("Diagnóstico do custo real das etiquetas — Nuvemshop")
    st.caption("Teste somente leitura. Inspeciona campos relacionados a frete, etiqueta e envio nos pedidos pagos, sem exibir dados pessoais ou tokens. Não presume que o frete cobrado é o custo da etiqueta.")
    if st.button("Investigar custo das etiquetas pela API", key="diag_etiquetas_api"):
        try:
            consultar_pedidos_nuvemshop.clear()
            pedidos_etiquetas = consultar_pedidos_nuvemshop()
            pagos_etiquetas = [p for p in pedidos_etiquetas if str(p.get("payment_status") or "").lower() == "paid"]
            palavras = ("ship", "freight", "label", "fulfill", "logistic", "delivery", "envio", "frete", "etiqueta", "tracking")
            def campos_relacionados(obj, prefixo="", profundidade=0):
                achados = []
                if profundidade > 3:
                    return achados
                if isinstance(obj, dict):
                    for chave, valor in obj.items():
                        caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
                        if any(palavra in str(chave).lower() for palavra in palavras):
                            tipo = "objeto" if isinstance(valor, dict) else "lista" if isinstance(valor, list) else "valor"
                            achados.append((caminho, tipo))
                        if isinstance(valor, dict):
                            achados.extend(campos_relacionados(valor, caminho, profundidade + 1))
                        elif isinstance(valor, list) and profundidade < 2:
                            for item in valor[:1]:
                                if isinstance(item, dict):
                                    achados.extend(campos_relacionados(item, caminho + "[]", profundidade + 1))
                return achados
            linhas_etiquetas = []
            for pedido_etiqueta in pagos_etiquetas:
                numero_etiqueta = str(pedido_etiqueta.get("number") or pedido_etiqueta.get("id") or "—")
                caminhos = campos_relacionados(pedido_etiqueta)
                campos_unicos = sorted({c for c, _ in caminhos})
                linhas_etiquetas.append({
                    "Pedido": numero_etiqueta,
                    "Frete cobrado do cliente (R$)": pedido_etiqueta.get("shipping_cost_customer"),
                    "Campos de envio disponíveis (nomes apenas)": ", ".join(campos_unicos) if campos_unicos else "Nenhum",
                })
            st.success(f"Diagnóstico concluído: {len(pagos_etiquetas)} pedido(s) pago(s) consultado(s).")
            if linhas_etiquetas:
                tabela_br(pd.DataFrame(linhas_etiquetas), hide_index=True, use_container_width=True)
                st.caption("Os nomes dos campos ajudam a localizar uma eventual informação de custo da etiqueta. A presença de um campo não comprova que contenha o valor efetivamente pago.")
                st.info("O valor da etiqueta ainda NÃO foi integrado ao cálculo financeiro. Quando o campo ou a fonte forem confirmados, faremos a conciliação por pedido.")
            else:
                st.warning("Não foram encontrados pedidos pagos para examinar.")
        except requests.HTTPError as erro:
            codigo = erro.response.status_code if erro.response is not None else "desconhecido"
            st.error(f"Consulta não concluída pela API (HTTP {codigo}).")
        except Exception:
            st.error("Não foi possível concluir o diagnóstico. Os dados financeiros existentes não foram alterados.")

    st.subheader("Diagnóstico aprofundado — valores de frete e etiqueta")
    st.caption("Somente leitura. Examina campos numéricos e metadados técnicos de envio dos pedidos pagos. Não exibe nomes, endereços, rastreamento, credenciais ou objetos completos da API.")
    if st.button("Inspecionar valores de envio pela API", key="diag_valores_envio"):
        try:
            consultar_pedidos_nuvemshop.clear()
            pedidos_envio = consultar_pedidos_nuvemshop()
            pagos_envio = [p for p in pedidos_envio if str(p.get("payment_status") or "").lower() == "paid"]
            termos_envio = ("ship", "freight", "label", "fulfill", "logistic", "delivery", "envio", "frete", "etiqueta", "cost", "price", "amount", "fee", "rate", "total")
            termos_proibidos = ("address", "street", "city", "zip", "postal", "phone", "email", "customer", "recipient", "name", "tracking", "token", "auth", "document", "cpf", "cnpj", "url", "link", "code", "number")

            def extrair_campos_envio(obj, prefixo="", nivel=0, ramo_envio=False):
                if nivel > 7:
                    return []
                achados = []
                if isinstance(obj, dict):
                    for chave, valor in obj.items():
                        nome = str(chave).lower()
                        if any(t in nome for t in termos_proibidos):
                            continue
                        caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
                        novo_ramo = ramo_envio or any(t in nome for t in ("ship", "freight", "label", "fulfill", "logistic", "delivery", "envio", "frete", "etiqueta"))
                        relevante = novo_ramo or (any(t in nome for t in termos_envio) and nivel == 0)
                        if relevante and isinstance(valor, (int, float, Decimal)) and not isinstance(valor, bool):
                            achados.append((caminho, str(valor)))
                        elif relevante and isinstance(valor, str) and any(t in nome for t in ("cost", "price", "amount", "fee", "rate", "total")):
                            try:
                                numero = Decimal(valor)
                                if numero.is_finite():
                                    achados.append((caminho, str(numero)))
                            except (InvalidOperation, ValueError):
                                pass
                        if isinstance(valor, (dict, list)):
                            achados.extend(extrair_campos_envio(valor, caminho, nivel + 1, novo_ramo))
                elif isinstance(obj, list):
                    for indice, item in enumerate(obj[:15]):
                        if isinstance(item, (dict, list)):
                            achados.extend(extrair_campos_envio(item, f"{prefixo}[{indice}]", nivel + 1, ramo_envio))
                return achados

            linhas_valores = []
            for pedido in pagos_envio:
                numero = str(pedido.get("number") or pedido.get("id") or "—")
                frete = pedido.get("shipping_cost_customer")
                achados = extrair_campos_envio(pedido)
                if achados:
                    for campo, valor in achados:
                        linhas_valores.append({"Pedido": numero, "Frete cobrado (R$)": frete, "Caminho técnico": campo, "Valor retornado": valor})
                else:
                    linhas_valores.append({"Pedido": numero, "Frete cobrado (R$)": frete, "Caminho técnico": "Nenhum campo numérico de envio encontrado", "Valor retornado": "—"})
            st.success(f"Consulta concluída: {len(pagos_envio)} pedido(s) pago(s) examinados.")
            if linhas_valores:
                tabela_valores = pd.DataFrame(linhas_valores)
                tabela_valores["Frete cobrado (R$)"] = tabela_valores["Frete cobrado (R$)"].map(
                    lambda v: moeda(valor_monetario(v)) if valor_monetario(v) is not None else "Pendente")
                tabela_br(tabela_valores, hide_index=True, use_container_width=True)
            st.warning("A presença de um valor em campos de envio NÃO comprova que seja o custo pago pela etiqueta. Precisamos validar o significado do campo e comparar com a etiqueta real do pedido #108 (R$ 14,18).")
            st.caption("Este diagnóstico não altera o cálculo financeiro nem grava dados dos pedidos.")
        except requests.HTTPError as erro:
            codigo = erro.response.status_code if erro.response is not None else "desconhecido"
            st.error(f"Não foi possível consultar os dados de envio (HTTP {codigo}).")
        except Exception:
            st.error("Falha ao examinar os campos de envio. Nenhum dado foi alterado.")

    st.subheader("Investigação financeira dos pagamentos — Nuvem Pago")
    st.caption("Leitura dos pedidos pagos da loja virtual. Sem alterações nos cálculos e sem exposição de dados pessoais ou credenciais.")
    if st.button("Verificar meios de pagamento e possíveis taxas", key="investigar_taxas_v2"):
        try:
            consultar_pedidos_nuvemshop.clear()
            pedidos_investigados = consultar_pedidos_nuvemshop()
            pagos_investigados = [p for p in pedidos_investigados if
                str(p.get("payment_status") or "").lower() == "paid" and
                str(p.get("status") or "").lower() not in ("cancelled", "canceled", "cancelado")]

            def texto_simples(valor):
                if isinstance(valor, (str, int, float, Decimal)) and not isinstance(valor, bool):
                    return str(valor)[:100] if str(valor).strip() else "Não informado"
                return "Não informado"

            def traduzir_metodo(valor):
                original = texto_simples(valor)
                chave = original.strip().lower()
                mapa = {"pix": "Pix", "credit_card": "Cartão de crédito", "debit_card": "Cartão de débito",
                        "boleto": "Boleto", "bank_slip": "Boleto", "cash": "Dinheiro", "transfer": "Transferência"}
                return mapa.get(chave, original)

            def analisar_possiveis_taxas(obj, caminho="", nivel=0):
                """Busca nomes explícitos de taxa/líquido sem expor identificadores ou outros dados."""
                if nivel > 6:
                    return []
                achados = []
                palavras = ("fee", "commission", "processing_cost", "transaction_cost", "net_amount",
                            "net_value", "net_total", "settlement_amount", "payout_amount", "taxa", "tarifa", "comissao", "líquido", "liquido")
                proibidos = ("token", "secret", "password", "key", "authorization", "customer", "buyer",
                             "email", "phone", "address", "document", "cpf", "cnpj", "card", "holder",
                             "account", "bank", "url", "tracking", "name", "code", "reference", "external_id")
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        nome = str(k).lower()
                        if any(t in nome for t in proibidos):
                            continue
                        novo = f"{caminho}.{k}" if caminho else str(k)
                        if isinstance(v, (dict, list)):
                            achados.extend(analisar_possiveis_taxas(v, novo, nivel + 1))
                        elif any(t in nome for t in palavras):
                            numero = valor_monetario(v)
                            if numero is not None:
                                achados.append({"Campo identificado": novo, "Valor informado": moeda(numero)})
                            else:
                                achados.append({"Campo identificado": novo, "Valor informado": "Não numérico ou não informado"})
                elif isinstance(obj, list):
                    for indice, item in enumerate(obj[:30]):
                        if isinstance(item, (dict, list)):
                            achados.extend(analisar_possiveis_taxas(item, f"{caminho}[{indice}]", nivel + 1))
                return achados

            resumo = []
            candidatos = []
            for pedido in pagos_investigados:
                numero = texto_simples(pedido.get("number") or pedido.get("id"))
                detalhes = pedido.get("payment_details")
                detalhes = detalhes if isinstance(detalhes, dict) else {}
                parcelas = detalhes.get("installments")
                try:
                    parcelas_texto = f"{int(float(parcelas))} parcela(s)" if parcelas is not None else "Não informado"
                except (ValueError, TypeError, OverflowError):
                    parcelas_texto = "Não informado"
                total = valor_monetario(pedido.get("total"))
                resumo.append({
                    "Pedido": numero,
                    "Total pago": moeda(total) if total is not None else "Não informado",
                    "Intermediador": texto_simples(pedido.get("gateway")),
                    "Meio de pagamento": traduzir_metodo(detalhes.get("method")),
                    "Parcelamento": parcelas_texto,
                    "Situação": "Pago",
                })
                for campo in analisar_possiveis_taxas(pedido):
                    candidatos.append({"Pedido": numero, **campo})

            st.success(f"Investigação concluída: {len(pagos_investigados)} pedido(s) pago(s) analisado(s).")
            st.markdown("**Resumo dos pagamentos**")
            tabela_br(pd.DataFrame(resumo), hide_index=True, use_container_width=True)
            st.markdown("**Campos candidatos a taxas e valores líquidos**")
            if candidatos:
                tabela_br(pd.DataFrame(candidatos), hide_index=True, use_container_width=True)
                st.warning("Os campos acima ainda precisam ser validados com o extrato do Nuvem Pago antes de entrar no Financeiro.")
            else:
                st.info("Nenhum campo explícito de taxa ou valor líquido foi encontrado nos dados dos pedidos. Isso não exclui a possibilidade de existir outra fonte financeira.")
            st.caption("Referência para conferência: pedido 108 — total R$ 126,68; taxa do Pix R$ 1,25; líquido R$ 125,43, conforme extrato informado.")
            st.caption("A consulta não altera taxas, pedidos, produtos ou resultados do BI.")
        except requests.HTTPError as erro:
            codigo = erro.response.status_code if erro.response is not None else "desconhecido"
            st.error(f"Não foi possível concluir a investigação (HTTP {codigo}).")
        except Exception:
            st.error("Falha na investigação dos pagamentos. Nenhum dado foi alterado.")

    st.subheader("Diagnóstico de custos de TODOS os produtos — Nuvemshop")
    st.caption("Consulta somente leitura de todo o catálogo, com paginação. Exibe produtos, variações, SKU, preço e custo retornados pela API. Não altera cadastros.")
    if st.button("Testar leitura de custos de todos os produtos"):
        try:
            produtos = consultar_produtos_nuvemshop_diagnostico()
            linhas = linhas_custo_diagnostico(produtos)
            if linhas:
                tabela_custos = pd.DataFrame(linhas)
                custos_numericos = pd.to_numeric(tabela_custos["Custo (API)"], errors="coerce")
                sem_custo = int(custos_numericos.isna().sum())
                st.success(f"Consulta concluída: {len(produtos)} produto(s) e {len(linhas)} variação(ões) encontradas.")
                if sem_custo:
                    st.warning(f"{sem_custo} variação(ões) sem custo numérico cadastrado. Não serão considerados como custo zero no Financeiro.")
                tabela_br(tabela_custos, use_container_width=True, hide_index=True)
                st.caption("Valores obtidos diretamente do campo 'cost' da API, não de uma tabela manual. Este é um diagnóstico; ainda não calcula o CPV por pedido.")
            else:
                st.warning("Nenhuma variação de produto foi retornada. Verifique os cadastros e permissões da API.")
        except requests.HTTPError as erro:
            codigo = erro.response.status_code if erro.response is not None else "desconhecido"
            st.error(f"A API não autorizou ou não concluiu a consulta de produtos (HTTP {codigo}). Pode ser necessária permissão adicional de leitura de produtos.")
        except Exception:
            st.error("Não foi possível consultar os custos dos produtos pela API. Nenhuma alteração foi feita.")

    st.divider()
    st.subheader("Origem do tráfego — GA4")
    st.caption("Dados reais dos últimos 7 dias completos (sem incluir hoje).")
    if st.button("Consultar canais de aquisição"):
        try:
            tabela = consultar_ga4_canais()
            if tabela.empty:
                st.info("Nenhum dado encontrado no período.")
            else:
                tabela_br(tabela, use_container_width=True, hide_index=True)
                st.bar_chart(tabela.set_index("Canal")[["Sessões"]], use_container_width=True)
                st.caption(f"Total de sessões por canal: {int(tabela['Sessões'].sum())}")
        except KeyError:
            st.error("Configuração GA4 ausente nos Segredos do Streamlit. Confira as seções ga4_service_account e ga4 (sem compartilhar as chaves).")
        except Exception:
            st.error("Não foi possível consultar os canais GA4. Confira as permissões da conta de serviço e a configuração da propriedade no Streamlit.")

rodape()
