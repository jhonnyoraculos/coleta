import os
import io
import datetime
from contextlib import contextmanager
from pathlib import Path
import base64
from decimal import Decimal
from typing import Optional

import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    create_engine,
    func,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, joinedload

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    HAS_REPORTLAB = True
except Exception:
    HAS_REPORTLAB = False

st.set_page_config(page_title="Controle de Coletas", layout="wide")

Base = declarative_base()
engine = None
SessionLocal = None

STATUS_OPCOES = ["PENDENTE", "EM ROTA", "CONCLUIDA", "CANCELADA"]


def fmt_dt(dt: Optional[datetime.datetime]) -> str:
    if not dt:
        return "-"
    try:
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(dt)


@st.cache_data(show_spinner=False)
def carregar_logo() -> Optional[bytes]:
    caminho = Path("logo-jr.png")
    if caminho.exists():
        try:
            return caminho.read_bytes()
        except Exception:
            return None
    return None


class Motorista(Base):
    __tablename__ = "motoristas"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    placa_principal = Column(String, nullable=True)
    telefone = Column(String, nullable=True)
    ativo = Column(Boolean, nullable=False, server_default="true", default=True)

    coletas = relationship("Coleta", back_populates="motorista")


class Coleta(Base):
    __tablename__ = "coletas"

    id = Column(Integer, primary_key=True, index=True)
    cliente = Column(String, nullable=False)
    local_coleta = Column(String, nullable=False)
    local_entrega = Column(String, nullable=True)
    data_combinada = Column(Date, nullable=False)
    prazo = Column(Date, nullable=False)
    motorista_id = Column(Integer, ForeignKey("motoristas.id"), nullable=False)
    placa_veiculo = Column(String, nullable=True)
    valor = Column(Numeric(10, 2), nullable=True)
    status = Column(String, nullable=False)
    observacoes = Column(String, nullable=True)
    criado_em = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    atualizado_em = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    concluido_em = Column(DateTime(timezone=True), nullable=True)
    comprovante_foto = Column(LargeBinary, nullable=True)
    comprovante_foto_nome = Column(String, nullable=True)
    comprovante_foto_mimetype = Column(String, nullable=True)

    motorista = relationship("Motorista", back_populates="coletas")


def get_engine():
    """Create and cache engine/session factory, ensuring env var exists."""
    global engine, SessionLocal
    if engine is not None and SessionLocal is not None:
        return engine

    db_url = os.getenv("DATABASE_URL")
    if not db_url and "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]

    if not db_url:
        st.error("Variavel DATABASE_URL nao encontrada. Configure no ambiente ou em .streamlit/secrets.toml.")
        st.stop()
    engine = create_engine(db_url)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,  # mantem atributos acessiveis apos commit/close
    )
    Base.metadata.create_all(bind=engine)
    return engine


@contextmanager
def get_session():
    """Context manager for DB sessions with rollback on error."""
    if SessionLocal is None:
        get_engine()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ------------------ CRUD Motoristas ------------------ #
def listar_motoristas(ativos_apenas=True):
    with get_session() as session:
        query = session.query(Motorista)
        if ativos_apenas:
            query = query.filter(Motorista.ativo.is_(True))
        return query.order_by(Motorista.nome.asc()).all()


def criar_motorista(nome, placa_principal=None, telefone=None):
    novo = Motorista(nome=nome.strip(), placa_principal=placa_principal or None, telefone=telefone or None, ativo=True)
    with get_session() as session:
        session.add(novo)


def alterar_status_motorista(motorista_id, ativo):
    with get_session() as session:
        motorista = session.query(Motorista).get(motorista_id)
        if motorista:
            motorista.ativo = ativo


# ------------------ CRUD Coletas ------------------ #
def criar_coleta(dados):
    coleta = Coleta(**dados)
    with get_session() as session:
        session.add(coleta)
        session.flush()  # garante que ID seja gerado antes do commit
        return coleta.id


def listar_coletas(status=None, motorista_id=None, prazo_inicio=None, prazo_fim=None):
    with get_session() as session:
        query = (
            session.query(Coleta)
            .options(joinedload(Coleta.motorista))
            .join(Motorista)
            .order_by(Coleta.prazo.asc(), Coleta.id.desc())
        )
        if status:
            query = query.filter(Coleta.status == status)
        if motorista_id:
            query = query.filter(Coleta.motorista_id == motorista_id)
        if prazo_inicio:
            query = query.filter(Coleta.prazo >= prazo_inicio)
        if prazo_fim:
            query = query.filter(Coleta.prazo <= prazo_fim)
        return query.all()


def obter_coleta_por_id(coleta_id):
    with get_session() as session:
        return (
            session.query(Coleta)
            .options(joinedload(Coleta.motorista))
            .get(coleta_id)
        )


def atualizar_coleta(coleta_id, campos, novo_status=None, foto=None):
    """
    Atualiza campos da coleta e lida com transição de status.
    foto: objeto retornado por st.file_uploader (ou None).
    """
    with get_session() as session:
        coleta = session.query(Coleta).get(coleta_id)
        if not coleta:
            return False, "Coleta não encontrada."

        # Impede mudar status se já concluída/cancelada
        status_atual = coleta.status
        if status_atual in ("CONCLUIDA", "CANCELADA") and novo_status and novo_status != status_atual:
            return False, "Coletas concluídas ou canceladas não podem ter o status alterado."

        for campo, valor in campos.items():
            setattr(coleta, campo, valor)

        agora = datetime.datetime.utcnow()
        coleta.atualizado_em = agora

        if novo_status and novo_status != status_atual:
            if novo_status not in STATUS_OPCOES:
                return False, "Status inválido."

            if novo_status == "CONCLUIDA":
                if foto is None:
                    return False, "Para marcar como concluída, você deve enviar uma foto de comprovante da carga."
                conteudo = foto.read()
                if not conteudo:
                    return False, "Arquivo de imagem inválido."
                coleta.comprovante_foto = conteudo
                coleta.comprovante_foto_nome = foto.name
                coleta.comprovante_foto_mimetype = foto.type
                coleta.concluido_em = agora
            coleta.status = novo_status

        return True, "Coleta atualizada com sucesso."


def gerar_pdf_protocolo(coleta, observacao: str) -> Optional[bytes]:
    """Cria um PDF simples do protocolo, pronto para impressão e assinatura manual."""
    if not HAS_REPORTLAB:
        return None
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 40

    def line(text, offset=16):
        nonlocal y
        c.drawString(40, y, text)
        y -= offset

    logo_bytes = carregar_logo()
    if logo_bytes:
        try:
            img = ImageReader(io.BytesIO(logo_bytes))
            c.drawImage(img, 40, height - 90, width=140, preserveAspectRatio=True, mask="auto")
            y = height - 110
        except Exception:
            y = height - 40

    c.setFont("Helvetica-Bold", 14)
    line(f"PROTOCOLO DE RECEBIMENTO - COLETA #{coleta.id}", 22)
    c.setFont("Helvetica", 11)
    line("Observacao: Conferir carga e registrar divergencias antes da assinatura.", 18)
    line(f"Cliente: {coleta.cliente}")
    line(f"Motorista: {coleta.motorista.nome if coleta.motorista else '-'}")
    line(f"Placa do veiculo: {coleta.placa_veiculo or '-'}")
    line(f"Local de coleta: {coleta.local_coleta}")
    line(f"Local de entrega: {coleta.local_entrega or '-'}")
    line(f"Data combinada: {coleta.data_combinada.strftime('%d/%m/%Y')}")
    line(f"Prazo: {coleta.prazo.strftime('%d/%m/%Y')}")
    line(f"Concluido em: {fmt_dt(coleta.concluido_em)}")
    line(f"Valor: {('R$ %.2f' % float(coleta.valor)) if coleta.valor is not None else '-'}")
    line(f"Observacoes do sistema: {coleta.observacoes or '-'}")
    line(f"Observacao adicional: {observacao or '-'}", 22)

    line("Assinaturas (manuais):", 18)
    line("Gestor da frota: _________________________________    Data recebimento: __________")
    line("Motorista: _________________________________", 26)

    c.setFont("Helvetica", 9)
    c.drawRightString(width - 40, 30, f"Gerado em {datetime.datetime.now():%d/%m/%Y %H:%M:%S}")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


# ------------------ UI helpers ------------------ #
def exibir_motoristas():
    st.header("Motoristas")
    col_filtro, _ = st.columns([1, 3])
    with col_filtro:
        somente_ativos = st.checkbox("Somente ativos", value=True)

    motoristas = listar_motoristas(ativos_apenas=somente_ativos)
    if motoristas:
        st.subheader("Lista de motoristas")
        for m in motoristas:
            col1, col2, col3 = st.columns([3, 2, 1])
            col1.write(f"**{m.nome}**")
            placa_info = m.placa_principal or "Sem placa"
            telefone_info = m.telefone or "-"
            col2.write(f"Placa: {placa_info} | Tel: {telefone_info}")
            novo_status = not m.ativo
            label_botao = "Desativar" if m.ativo else "Ativar"
            if col3.button(label_botao, key=f"toggle_{m.id}"):
                alterar_status_motorista(m.id, novo_status)
                st.rerun()
    else:
        st.info("Nenhum motorista cadastrado.")

    st.subheader("Cadastrar novo motorista")
    with st.form("form_motorista", clear_on_submit=True):
        nome = st.text_input("Nome*", max_chars=100)
        placa = st.text_input("Placa principal (opcional)", max_chars=20)
        telefone = st.text_input("Telefone (opcional)", max_chars=30)
        enviado = st.form_submit_button("Salvar motorista")

        if enviado:
            if not nome.strip():
                st.error("Nome é obrigatório.")
            else:
                try:
                    criar_motorista(nome, placa_principal=placa, telefone=telefone)
                    st.success("Motorista salvo com sucesso.")
                except SQLAlchemyError as e:
                    st.error(f"Erro ao salvar motorista: {e}")


def exibir_nova_coleta():
    st.header("Nova Coleta")
    motoristas = listar_motoristas(ativos_apenas=True)
    if not motoristas:
        st.warning("Cadastre um motorista ativo antes de criar uma coleta.")
        return

    with st.form("form_coleta"):
        cliente = st.text_input("Cliente*", max_chars=200)
        local_coleta = st.text_input("Local de coleta*", max_chars=200)
        local_entrega = st.text_input("Local de entrega (opcional)", max_chars=200)
        data_combinada = st.date_input("Data combinada*", value=datetime.date.today())
        prazo = st.date_input("Prazo*", value=datetime.date.today())

        motorista_nomes = [f"{m.nome} (#{m.id})" for m in motoristas]
        motorista_escolhido = st.selectbox("Motorista*", motorista_nomes)
        motorista_id = motoristas[motorista_nomes.index(motorista_escolhido)].id
        placa_veiculo = st.text_input(
            "Placa do veículo (opcional)",
            value=next((m.placa_principal for m in motoristas if m.id == motorista_id and m.placa_principal), ""),
            max_chars=20,
        )
        valor = st.number_input("Valor (opcional)", min_value=0.0, step=0.01, format="%.2f")
        observacoes = st.text_area("Observações (opcional)", max_chars=1000)

        enviado = st.form_submit_button("Salvar coleta")

        if enviado:
            campos_obrigatorios = [cliente.strip(), local_coleta.strip(), data_combinada, prazo, motorista_id]
            if not all(campos_obrigatorios):
                st.error("Preencha todos os campos obrigatórios.")
            else:
                dados = {
                    "cliente": cliente.strip(),
                    "local_coleta": local_coleta.strip(),
                    "local_entrega": local_entrega.strip() or None,
                    "data_combinada": data_combinada,
                    "prazo": prazo,
                    "motorista_id": motorista_id,
                    "placa_veiculo": placa_veiculo.strip() or None,
                    "valor": Decimal(valor) if valor else None,
                    "status": "PENDENTE",
                    "observacoes": observacoes.strip() or None,
                }
                try:
                    novo_id = criar_coleta(dados)
                    st.success(f"Coleta criada com sucesso. ID: {novo_id}")
                except SQLAlchemyError as e:
                    st.error(f"Erro ao salvar coleta: {e}")


def _format_indicador(coleta):
    hoje = datetime.date.today()
    if coleta.status == "PENDENTE":
        if coleta.prazo < hoje:
            return "ATRASADA"
        if coleta.prazo == hoje:
            return "PRAZO HOJE"
    if coleta.status == "CONCLUIDA":
        return "Concluída"
    if coleta.status == "EM ROTA":
        return "Em rota"
    if coleta.status == "CANCELADA":
        return "Cancelada"
    return ""


def exibir_coletas():
    st.header("Coletas")

    status_map = {
        "Todas": None,
        "Pendentes": "PENDENTE",
        "Em rota": "EM ROTA",
        "Concluídas": "CONCLUIDA",
        "Canceladas": "CANCELADA",
    }

    motoristas_todos = listar_motoristas(ativos_apenas=False)
    motoristas_options = ["Todos"] + [f"{m.nome} (#{m.id})" for m in motoristas_todos]

    col1, col2, col3, col4 = st.columns(4)
    filtro_status = col1.selectbox("Status", list(status_map.keys()))
    filtro_motorista_txt = col2.selectbox("Motorista", motoristas_options)
    usar_prazo = col3.checkbox("Filtrar por prazo")
    prazo_inicio = None
    prazo_fim = None
    if usar_prazo:
        prazo_inicio = col3.date_input("Prazo inicial", value=datetime.date.today())
        prazo_fim = col4.date_input("Prazo final", value=datetime.date.today())

    motorista_id = None
    if filtro_motorista_txt != "Todos":
        motorista_id = int(filtro_motorista_txt.split("#")[-1].strip(")"))

    coletas = listar_coletas(
        status=status_map[filtro_status],
        motorista_id=motorista_id,
        prazo_inicio=prazo_inicio,
        prazo_fim=prazo_fim,
    )

    if coletas:
        st.subheader("Lista filtrada")
        dados = []
        for c in coletas:
            dados.append(
                {
                    "ID": c.id,
                    "Cliente": c.cliente,
                    "Motorista": c.motorista.nome if c.motorista else "-",
                    "Prazo": c.prazo.strftime("%d/%m/%Y"),
                    "Valor": float(c.valor) if c.valor is not None else None,
                    "Status": c.status,
                    "Indicador": _format_indicador(c),
                }
            )

        try:
            import pandas as pd

            df = pd.DataFrame(dados)

            def highlight_row(row):
                indicador = row["Indicador"]
                if indicador == "ATRASADA":
                    return ["background-color: #ffcccc"] * len(row)
                if indicador == "PRAZO HOJE":
                    return ["background-color: #fff3cd"] * len(row)
                if row["Status"] == "CONCLUIDA":
                    return ["color: gray"] * len(row)
                return [""] * len(row)

            st.dataframe(df.style.apply(highlight_row, axis=1), use_container_width=True)
        except ImportError:
            st.table(dados)

        opcoes = [f"{c.id} - {c.cliente}" for c in coletas]
        escolha = st.selectbox("Selecione uma coleta para ver detalhes", options=opcoes)
        coleta_id = int(escolha.split(" - ")[0])
        coleta = next((c for c in coletas if c.id == coleta_id), None)
        if coleta:
            exibir_detalhe_coleta(coleta)
    else:
        st.info("Nenhuma coleta encontrada com os filtros selecionados.")


def exibir_detalhe_coleta(coleta):
    st.subheader(f"Detalhes da coleta #{coleta.id}")
    col1, col2 = st.columns(2)

    col1.markdown(
        f"""
        **Cliente:** {coleta.cliente}  
        **Local coleta:** {coleta.local_coleta}  
        **Local entrega:** {coleta.local_entrega or '-'}  
        **Data combinada:** {coleta.data_combinada.strftime('%d/%m/%Y')}  
        **Prazo:** {coleta.prazo.strftime('%d/%m/%Y')}  
        **Motorista:** {coleta.motorista.nome if coleta.motorista else '-'}  
        **Placa veiculo:** {coleta.placa_veiculo or '-'}  
        """
    )

    col2.markdown(
        f"""
        **Valor:** {('R$ %.2f' % float(coleta.valor)) if coleta.valor is not None else '-'}  
        **Status:** {coleta.status}  
        **Observacoes:** {coleta.observacoes or '-'}  
        **Criado em:** {fmt_dt(coleta.criado_em)}  
        **Atualizado em:** {fmt_dt(coleta.atualizado_em)}  
        **Concluido em:** {fmt_dt(coleta.concluido_em)}  
        """
    )

    if coleta.comprovante_foto:
        st.image(
            io.BytesIO(coleta.comprovante_foto),
            caption=coleta.comprovante_foto_nome or "Comprovante",
            use_column_width=True,
        )

    st.markdown("---")
    pode_editar_status = coleta.status not in ("CONCLUIDA", "CANCELADA")

    motoristas_ativos = listar_motoristas(ativos_apenas=True)
    if not motoristas_ativos:
        st.warning("Nenhum motorista ativo para edicao.")
        return

    motoristas_opts = [f"{m.nome} (#{m.id})" for m in motoristas_ativos]
    motorista_default_idx = next((i for i, m in enumerate(motoristas_ativos) if m.id == coleta.motorista_id), 0)

    with st.form(f"editar_coleta_{coleta.id}"):
        cliente = st.text_input("Cliente*", value=coleta.cliente)
        local_coleta = st.text_input("Local de coleta*", value=coleta.local_coleta)
        local_entrega = st.text_input("Local de entrega", value=coleta.local_entrega or "")
        data_combinada = st.date_input("Data combinada*", value=coleta.data_combinada)
        prazo = st.date_input("Prazo*", value=coleta.prazo)

        motorista_txt = st.selectbox("Motorista*", options=motoristas_opts, index=motorista_default_idx)
        motorista_id = motoristas_ativos[motoristas_opts.index(motorista_txt)].id

        placa_veiculo = st.text_input("Placa do veiculo", value=coleta.placa_veiculo or "")
        valor = st.number_input(
            "Valor (opcional)",
            min_value=0.0,
            step=0.01,
            format="%.2f",
            value=float(coleta.valor) if coleta.valor is not None else 0.0,
        )
        observacoes = st.text_area("Observacoes", value=coleta.observacoes or "", max_chars=1000)

        novo_status = coleta.status
        foto = None
        if pode_editar_status:
            novo_status = st.selectbox("Status", options=STATUS_OPCOES, index=STATUS_OPCOES.index(coleta.status))
            foto = st.file_uploader(
                "Foto de comprovante da carga (obrigatorio ao concluir)",
                type=["jpg", "jpeg", "png"],
                key=f"foto_{coleta.id}",
            )
        else:
            st.info("Status CONCLUIDA ou CANCELADA nao pode ser alterado.")

        salvar = st.form_submit_button("Salvar alteracoes")

        if salvar:
            if not cliente.strip() or not local_coleta.strip() or not data_combinada or not prazo or not motorista_id:
                st.error("Preencha todos os campos obrigatorios.")
                return

            campos = {
                "cliente": cliente.strip(),
                "local_coleta": local_coleta.strip(),
                "local_entrega": local_entrega.strip() or None,
                "data_combinada": data_combinada,
                "prazo": prazo,
                "motorista_id": motorista_id,
                "placa_veiculo": placa_veiculo.strip() or None,
                "valor": Decimal(valor) if valor else None,
                "observacoes": observacoes.strip() or None,
            }

            sucesso, mensagem = atualizar_coleta(coleta.id, campos, novo_status=novo_status if pode_editar_status else None, foto=foto)
            if sucesso:
                st.success(mensagem)
                st.rerun()
            else:
                st.error(mensagem)


def exibir_concluidas():
    st.header("Coletas concluidas")
    coletas = listar_coletas(status="CONCLUIDA")
    if not coletas:
        st.info("Nenhuma coleta concluida encontrada.")
        return

    opcoes = [f"{c.id} - {c.cliente}" for c in coletas]
    escolha = st.selectbox("Selecione uma coleta concluida", options=opcoes)
    coleta_id = int(escolha.split(" - ")[0])
    coleta = next((c for c in coletas if c.id == coleta_id), None)
    if not coleta:
        st.error("Coleta nao encontrada.")
        return

    st.subheader(f"Resumo #{coleta.id}")
    col1, col2 = st.columns(2)
    col1.markdown(
        f"""
        **Cliente:** {coleta.cliente}  
        **Motorista:** {coleta.motorista.nome if coleta.motorista else '-'}  
        **Placa veiculo:** {coleta.placa_veiculo or '-'}  
        **Local coleta:** {coleta.local_coleta}  
        **Local entrega:** {coleta.local_entrega or '-'}  
        **Data combinada:** {coleta.data_combinada.strftime('%d/%m/%Y')}  
        **Prazo:** {coleta.prazo.strftime('%d/%m/%Y')}  
        """
    )
    col2.markdown(
        f"""
        **Valor:** {('R$ %.2f' % float(coleta.valor)) if coleta.valor is not None else '-'}  
        **Status:** {coleta.status}  
        **Observacoes:** {coleta.observacoes or '-'}  
        **Criado em:** {fmt_dt(coleta.criado_em)}  
        **Concluido em:** {fmt_dt(coleta.concluido_em)}  
        """
    )

    if coleta.comprovante_foto:
        st.image(
            io.BytesIO(coleta.comprovante_foto),
            caption=coleta.comprovante_foto_nome or "Comprovante",
            use_column_width=True,
        )

    st.markdown("---")
    st.subheader("Protocolo para impressao")
    with st.form(f"protocolo_{coleta.id}"):
        observacao_extra = st.text_area(
            "Observacao adicional (opcional, sera impressa no protocolo)",
            value="Conferir carga; assinatura manual do gestor da frota e motorista.",
            max_chars=500,
        )
        submit = st.form_submit_button("Gerar protocolo")

    if submit:
        # Assinaturas sempre manuais: nao inserimos nomes
        protocolo = f"""
PROTOCOLO DE ENTREGA - COLETA #{coleta.id}
-------------------------------------------
Cliente: {coleta.cliente}
Motorista: {coleta.motorista.nome if coleta.motorista else '-'}
Placa do veiculo: {coleta.placa_veiculo or '-'}
Local de coleta: {coleta.local_coleta}
Local de entrega: {coleta.local_entrega or '-'}
Data combinada: {coleta.data_combinada.strftime('%d/%m/%Y')}
Prazo: {coleta.prazo.strftime('%d/%m/%Y')}
Concluido em: {coleta.concluido_em}
Valor: {('R$ %.2f' % float(coleta.valor)) if coleta.valor is not None else '-'}
Observacoes: {coleta.observacoes or '-'}
Observacao adicional: {observacao_extra or '-'}

Confirmacao de entrega:
- Gestor da frota: ______________________________________    Data recebimento: __________
- Motorista: ____________________________________________

Assinaturas:
Gestor: _________________________________    Data: __________________
Colaborador: ____________________________    Data: __________________
"""
        st.success("Protocolo gerado. Baixe ou imprima.")
        st.download_button(
            "Baixar protocolo (TXT)",
            data=protocolo,
            file_name=f"protocolo_coleta_{coleta.id}.txt",
            mime="text/plain",
        )
        st.text_area("Visualizacao do protocolo", protocolo, height=300)

        # PDF profissional
        pdf_bytes = gerar_pdf_protocolo(coleta, observacao_extra)
        if pdf_bytes:
            st.download_button(
                "Baixar protocolo (PDF)",
                data=pdf_bytes,
                file_name=f"protocolo_coleta_{coleta.id}.pdf",
                mime="application/pdf",
            )
            b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
            pdf_data_url = f"data:application/pdf;base64,{b64_pdf}"
            if st.button("Imprimir protocolo agora"):
                components.html(
                    f"""
                    <iframe id="printFrame" src="{pdf_data_url}" style="width:0;height:0;border:0;"></iframe>
                    <script>
                    const f = document.getElementById('printFrame');
                    f.onload = () => {{
                        try {{
                            f.contentWindow.focus();
                            f.contentWindow.print();
                        }} catch (e) {{
                            alert("Não foi possível abrir o PDF para impressão. Tente baixar o PDF e imprimir manualmente.");
                        }}
                    }};
                    </script>
                    """,
                    height=0,
                    width=0,
                )
        else:
            st.warning("Para gerar PDF, instale a dependencia: pip install reportlab")

        st.info("Para imprimir, baixe o PDF (ou TXT) e envie para a impressora.")


def main():
    get_engine()
    logo = carregar_logo()
    if logo:
        st.image(logo, width=140)
    st.title("Controle de Coletas")

    pagina = st.sidebar.radio("Navegacao", ("Coletas", "Concluidas", "Nova Coleta", "Motoristas"))

    if pagina == "Motoristas":
        exibir_motoristas()
    elif pagina == "Concluidas":
        exibir_concluidas()
    elif pagina == "Nova Coleta":
        exibir_nova_coleta()
    else:
        exibir_coletas()


if __name__ == "__main__":
    main()
