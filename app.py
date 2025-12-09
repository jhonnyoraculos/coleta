import os
import io
import datetime
from contextlib import contextmanager
from pathlib import Path
import base64
from decimal import Decimal
from typing import Optional
import zipfile
import mimetypes
import os
import hashlib
import hmac
import secrets

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

try:
    from PIL import Image

    HAS_PIL = True
except Exception:
    HAS_PIL = False

st.set_page_config(page_title="Controle de Coletas", layout="wide")

Base = declarative_base()
engine = None
SessionLocal = None

STATUS_OPCOES = ["PENDENTE", "CONCLUIDA", "CANCELADA"]
MAX_IMG_BYTES = 650 * 1024  # ~650 KB
BACKUP_THRESHOLD_BYTES = 360 * 1024 * 1024  # 360 MB


def resumo_coletas():
    """Retorna contagem por status para mostrar KPIs rapidos."""
    if SessionLocal is None:
        get_engine()
    with get_session() as session:
        total = session.query(func.count(Coleta.id)).scalar()
        pend = session.query(func.count(Coleta.id)).filter(Coleta.status == "PENDENTE").scalar()
        rota = session.query(func.count(Coleta.id)).filter(Coleta.status == "EM ROTA").scalar()
        concl = session.query(func.count(Coleta.id)).filter(Coleta.status == "CONCLUIDA").scalar()
        canc = session.query(func.count(Coleta.id)).filter(Coleta.status == "CANCELADA").scalar()
    return {
        "total": total or 0,
        "pendente": pend or 0,
        "em_rota": rota or 0,
        "concluida": concl or 0,
        "cancelada": canc or 0,
    }


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


class Caminhao(Base):
    __tablename__ = "caminhoes"

    id = Column(Integer, primary_key=True, index=True)
    placa = Column(String, nullable=False, unique=True)
    modelo = Column(String, nullable=True)
    ano = Column(Integer, nullable=True)
    ativo = Column(Boolean, nullable=False, server_default="true", default=True)


class ColetaExcluida(Base):
    __tablename__ = "coletas_excluidas"

    id = Column(Integer, primary_key=True, index=True)
    coleta_id = Column(Integer, nullable=False)
    cliente = Column(String, nullable=False)
    local_coleta = Column(String, nullable=False)
    local_entrega = Column(String, nullable=True)
    motorista_nome = Column(String, nullable=True)
    placa_veiculo = Column(String, nullable=True)
    valor = Column(Numeric(10, 2), nullable=True)
    status = Column(String, nullable=False)
    observacoes = Column(String, nullable=True)
    data_combinada = Column(Date, nullable=True)
    prazo = Column(Date, nullable=True)
    criado_em = Column(DateTime(timezone=True), nullable=True)
    concluido_em = Column(DateTime(timezone=True), nullable=True)
    deletado_em = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    senha_hash = Column(String, nullable=False)
    ativo = Column(Boolean, nullable=False, server_default="true", default=True)


def gerar_zip_concluidas():
    """Cria um zip com PDF, comprovantes e resumo TXT de cada coleta concluida."""
    if not HAS_REPORTLAB:
        return False, "Instale reportlab para gerar os PDFs."
    coletas = listar_coletas(status="CONCLUIDA")
    if not coletas:
        return False, "Nenhuma coleta concluida para exportar."

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for c in coletas:
            pdf = gerar_pdf_protocolo(c, "Backup gerado pelo sistema.")
            if pdf:
                zf.writestr(f"coleta_{c.id}.pdf", pdf)
            if c.comprovante_foto:
                ext = ""
                if c.comprovante_foto_nome and "." in c.comprovante_foto_nome:
                    ext = "." + c.comprovante_foto_nome.split(".")[-1]
                elif c.comprovante_foto_mimetype:
                    ext = mimetypes.guess_extension(c.comprovante_foto_mimetype) or ""
                else:
                    ext = ".bin"
                zf.writestr(f"coleta_{c.id}_comprovante{ext}", c.comprovante_foto)
            resumo_txt = f"""
ID: {c.id}
Cliente: {c.cliente}
Motorista: {c.motorista.nome if c.motorista else '-'}
Placa: {c.placa_veiculo or '-'}
Local coleta: {c.local_coleta}
Local entrega: {c.local_entrega or '-'}
Material/Obs: {c.observacoes or '-'}
Data combinada: {c.data_combinada.strftime('%d/%m/%Y')}
Prazo: {c.prazo.strftime('%d/%m/%Y')}
Criado em: {fmt_dt(c.criado_em)}
Concluido em: {fmt_dt(c.concluido_em)}
Status: {c.status}
Valor: {('R$ %.2f' % float(c.valor)) if c.valor is not None else '-'}
""".strip()
            zf.writestr(f"coleta_{c.id}_resumo.txt", resumo_txt)
    buf.seek(0)
    return True, buf.getvalue()


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
    novo = Motorista(nome=nome.strip(), telefone=telefone or None, ativo=True)
    with get_session() as session:
        session.add(novo)


def alterar_status_motorista(motorista_id, ativo):
    with get_session() as session:
        motorista = session.query(Motorista).get(motorista_id)
        if motorista:
            motorista.ativo = ativo


def motorista_em_uso(motorista_id):
    with get_session() as session:
        return session.query(Coleta.id).filter(Coleta.motorista_id == motorista_id).first() is not None


def caminhao_em_uso(placa):
    if not placa:
        return False
    with get_session() as session:
        return session.query(Coleta.id).filter(Coleta.placa_veiculo == placa).first() is not None


# ------------------ Auth helpers ------------------ #
def _hash_senha(senha: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, 120000)
    return salt.hex() + ":" + dk.hex()


def _verificar_senha(senha: str, senha_hash: str) -> bool:
    try:
        salt_hex, hash_hex = senha_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        esperado = bytes.fromhex(hash_hex)
        calculado = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), salt, 120000)
        return hmac.compare_digest(calculado, esperado)
    except Exception:
        return False


def criar_usuario(username: str, senha: str):
    with get_session() as session:
        if session.query(Usuario).filter(Usuario.username == username).first():
            return False, "Usuario ja existe."
        u = Usuario(username=username.strip().lower(), senha_hash=_hash_senha(senha), ativo=True)
        session.add(u)
    return True, "Usuario criado."


def autenticar(username: str, senha: str):
    with get_session() as session:
        u = session.query(Usuario).filter(Usuario.username == username.strip().lower(), Usuario.ativo.is_(True)).first()
        if not u:
            return False
        return _verificar_senha(senha, u.senha_hash)


def garantir_admin_padrao():
    """Cria admin padrao se nao existir usuario algum."""
    with get_session() as session:
        existe = session.query(Usuario).first()
        if existe:
            return
    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin")
    criar_usuario(admin_user, admin_pass)


# ------------------ CRUD Caminhoes ------------------ #
def listar_caminhoes(ativos_apenas=True):
    with get_session() as session:
        query = session.query(Caminhao)
        if ativos_apenas:
            query = query.filter(Caminhao.ativo.is_(True))
        return query.order_by(Caminhao.placa.asc()).all()


def criar_caminhao(placa, modelo=None, ano=None):
    novo = Caminhao(placa=placa.strip().upper(), modelo=modelo or None, ano=ano or None, ativo=True)
    with get_session() as session:
        session.add(novo)


def alterar_status_caminhao(caminhao_id, ativo):
    with get_session() as session:
        cam = session.query(Caminhao).get(caminhao_id)
        if cam:
            cam.ativo = ativo


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
            if isinstance(status, (list, tuple, set)):
                query = query.filter(Coleta.status.in_(status))
            else:
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
    Atualiza campos da coleta e lida com transicao de status.
    foto: objeto retornado por st.file_uploader (ou None).
    """
    with get_session() as session:
        coleta = session.query(Coleta).get(coleta_id)
        if not coleta:
            return False, "Coleta nao encontrada."

        status_atual = coleta.status
        if status_atual in ("CONCLUIDA", "CANCELADA"):
            if novo_status and novo_status != status_atual:
                return False, "Coletas concluidas ou canceladas nao podem ter o status alterado."
            return False, "Coletas concluidas ou canceladas nao podem ser editadas."

        for campo, valor in campos.items():
            setattr(coleta, campo, valor)

        agora = datetime.datetime.utcnow()
        coleta.atualizado_em = agora

        if novo_status and novo_status != status_atual:
            if novo_status not in STATUS_OPCOES:
                return False, "Status invalido."

            if novo_status == "CONCLUIDA":
                if foto is None:
                    return False, "Para marcar como concluida, voce deve enviar uma foto de comprovante da carga."
                ok_img, resultado_img = processar_foto_comprovante(foto)
                if not ok_img:
                    return False, resultado_img
                conteudo, nome_img, mimetype_img = resultado_img
                coleta.comprovante_foto = conteudo
                coleta.comprovante_foto_nome = nome_img
                coleta.comprovante_foto_mimetype = mimetype_img
                coleta.concluido_em = agora
            coleta.status = novo_status

        return True, "Coleta atualizada com sucesso."


def remover_coleta(coleta_id):
    with get_session() as session:
        coleta = session.query(Coleta).options(joinedload(Coleta.motorista)).get(coleta_id)
        if not coleta:
            return False, "Coleta nao encontrada."
        arquivada = ColetaExcluida(
            coleta_id=coleta.id,
            cliente=coleta.cliente,
            local_coleta=coleta.local_coleta,
            local_entrega=coleta.local_entrega,
            motorista_nome=coleta.motorista.nome if coleta.motorista else None,
            placa_veiculo=coleta.placa_veiculo,
            valor=coleta.valor,
            status=coleta.status,
            observacoes=coleta.observacoes,
            data_combinada=coleta.data_combinada,
            prazo=coleta.prazo,
            criado_em=coleta.criado_em,
            concluido_em=coleta.concluido_em,
        )
        session.add(arquivada)
        session.delete(coleta)
        return True, "Coleta excluida com sucesso."


def remover_todas_coletas():
    """Remove todas as coletas, arquivando no log de excluidas."""
    coletas = listar_coletas()
    total = 0
    for c in coletas:
        ok, _ = remover_coleta(c.id)
        if ok:
            total += 1
    return total


def gerar_pdf_protocolo(coleta, observacao: str) -> Optional[bytes]:
    """Cria um PDF simples do protocolo, pronto para impressao e assinatura manual."""
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
            img_w, img_h = img.getSize()
            target_w = 120
            target_h = target_w * (img_h / img_w)
            x_pos = (width - target_w) / 2
            c.drawImage(img, x_pos, height - target_h - 20, width=target_w, height=target_h, preserveAspectRatio=True, mask="auto")
            y = height - target_h - 35
        except Exception:
            y = height - 40

    c.setFont("Helvetica-Bold", 14)
    line(f"PROTOCOLO DE RECEBIMENTO - COLETA #{coleta.id}", 22)
    c.setFont("Helvetica", 11)
    line("Observacao: Conferir a carga e registrar divergencias antes da assinatura.", 18)
    line(f"Cliente: {coleta.cliente}")
    line(f"Motorista: {coleta.motorista.nome if coleta.motorista else '-'}")
    line(f"Placa do veiculo: {coleta.placa_veiculo or '-'}")
    line(f"Local de coleta: {coleta.local_coleta}")
    line(f"Local de entrega: {coleta.local_entrega or '-'}")
    line(f"Material/Carga: {coleta.observacoes or '-'}")
    line(f"Data de emissao: {datetime.date.today().strftime('%d/%m/%Y')}")
    line(f"Observacao adicional: {observacao or '-'}", 22)

    c.setFont("Helvetica", 9)
    c.drawRightString(width - 40, 30, f"Gerado em {datetime.datetime.now():%d/%m/%Y %H:%M:%S}")

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()


def montar_protocolo_txt(coleta, observacao_extra: str) -> str:
    """Monta texto simples de protocolo para download ou envio rapido."""
    return f"""
PROTOCOLO DE ENTREGA - COLETA #{coleta.id}
Cliente: {coleta.cliente}
Motorista: {coleta.motorista.nome if coleta.motorista else '-'}
Placa do veiculo: {coleta.placa_veiculo or '-'}
Local de coleta: {coleta.local_coleta}
Local de entrega: {coleta.local_entrega or '-'}
Material/carga: {coleta.observacoes or '-'}
Data de emissao: {datetime.date.today().strftime('%d/%m/%Y')}
Observacao adicional: {observacao_extra or '-'}

Confirmacao de entrega:
- Gestor da frota: ______________________________________    Data recebimento: __________
- Motorista: ____________________________________________

Assinaturas:
Gestor: _________________________________    Data: __________________
Colaborador: ____________________________    Data: __________________
""".strip()


def _comprimir_imagem_ate_1mb(conteudo: bytes):
    """Tenta comprimir/redimensionar a imagem para ficar <= 1 MB."""
    if not HAS_PIL:
        return False, "Para comprimir imagens maiores que 1MB, instale pillow: pip install pillow"
    try:
        img = Image.open(io.BytesIO(conteudo)).convert("RGB")
    except Exception:
        return False, "Nao foi possivel ler a imagem enviada."

    def salvar(img_to_save, qualidade):
        buf = io.BytesIO()
        img_to_save.save(buf, format="JPEG", quality=qualidade, optimize=True)
        return buf

    for qualidade in (85, 70, 55, 40, 30):
        buf = salvar(img, qualidade)
        if buf.tell() <= MAX_IMG_BYTES:
            return True, buf.getvalue(), "image/jpeg"

    # Redimensiona proporcionalmente e tenta novamente
    fator = (MAX_IMG_BYTES / max(1, buf.tell())) ** 0.5
    novo_tam = (max(1, int(img.width * fator)), max(1, int(img.height * fator)))
    img_red = img.resize(novo_tam)
    for qualidade in (75, 60, 50, 40, 30):
        buf = salvar(img_red, qualidade)
        if buf.tell() <= MAX_IMG_BYTES:
            return True, buf.getvalue(), "image/jpeg"

    return False, "Nao foi possivel comprimir a imagem para 1MB. Envie uma imagem menor."


def processar_foto_comprovante(foto):
    """Le e comprime foto para no maximo 1MB."""
    if foto is None:
        return False, "Foto obrigatoria."
    conteudo = foto.read()
    if not conteudo:
        return False, "Arquivo de imagem invalido."
    if len(conteudo) <= MAX_IMG_BYTES:
        return True, (conteudo, foto.name, foto.type or "image/octet-stream")

    resultado = _comprimir_imagem_ate_1mb(conteudo)
    if not resultado[0]:
        return False, resultado[1]
    _, conteudo_comp, mimetype_comp = resultado
    return True, (conteudo_comp, foto.name, mimetype_comp)


# ------------------ UI helpers ------------------ #
def exibir_motoristas():
    st.header("Motoristas")
    col_filtro, _ = st.columns([1, 3])
    with col_filtro:
        somente_ativos = st.checkbox("Somente ativos", value=True)

    motoristas = listar_motoristas(ativos_apenas=somente_ativos)
    busca = st.text_input("Buscar motoristas (nome ou telefone)", "")
    if busca:
        termo = busca.lower().strip()
        motoristas = [m for m in motoristas if termo in m.nome.lower() or termo in (m.telefone or "").lower()]

    if motoristas:
        st.subheader("Lista de motoristas")
        for m in motoristas:
            with st.expander(f"{m.nome}"):
                em_uso = motorista_em_uso(m.id)
                col1, col2 = st.columns([4, 1])
                with col1:
                    nome_edit = st.text_input("Nome", value=m.nome, key=f"nome_m_{m.id}", disabled=em_uso)
                    telefone_edit = st.text_input("Telefone", value=m.telefone or "", key=f"tel_m_{m.id}", disabled=em_uso)
                with col2:
                    novo_status = not m.ativo
                    label_botao = "Excluir" if m.ativo else "Restaurar"
                    if st.button(label_botao, key=f"toggle_{m.id}", disabled=em_uso):
                        alterar_status_motorista(m.id, novo_status)
                        st.rerun()
                    if st.button("Apagar", key=f"del_motorista_{m.id}", disabled=em_uso):
                        with get_session() as session:
                            mot = session.query(Motorista).get(m.id)
                            if mot:
                                session.delete(mot)
                        st.rerun()
                if em_uso:
                    st.info("Motorista em uso em coletas. Edicao e exclusao bloqueadas.")
                if st.button("Salvar edicao", key=f"save_m_{m.id}", disabled=em_uso):
                    with get_session() as session:
                        mot = session.query(Motorista).get(m.id)
                        if mot:
                            mot.nome = nome_edit.strip() or mot.nome
                            mot.telefone = telefone_edit.strip() or None
                    st.success("Motorista atualizado.")
                    st.rerun()
    else:
        st.info("Nenhum motorista cadastrado.")

    st.subheader("Cadastrar novo motorista")
    with st.form("form_motorista", clear_on_submit=True):
        nome = st.text_input("Nome*", max_chars=100)
        telefone = st.text_input("Telefone (opcional)", max_chars=30)
        enviado = st.form_submit_button("Salvar motorista")

        if enviado:
            if not nome.strip():
                st.error("Nome e obrigatorio.")
            else:
                try:
                    criar_motorista(nome, telefone=telefone)
                    st.success("Motorista salvo com sucesso.")
                except SQLAlchemyError as e:
                    st.error(f"Erro ao salvar motorista: {e}")


def exibir_caminhoes():
    st.header("Caminhoes")
    col_filtro, _ = st.columns([1, 3])
    with col_filtro:
        somente_ativos = st.checkbox("Somente ativos", value=True, key="caminhoes_ativos")

    caminhoes = listar_caminhoes(ativos_apenas=somente_ativos)
    busca = st.text_input("Buscar caminhoes (placa/modelo)", "", key="busca_cam")
    if busca:
        termo = busca.lower().strip()
        caminhoes = [c for c in caminhoes if termo in c.placa.lower() or termo in (c.modelo or "").lower()]

    if caminhoes:
        st.subheader("Lista de caminhoes")
        for cam in caminhoes:
            with st.expander(f"{cam.placa} - {cam.modelo or '-'} ({cam.ano or '-'})"):
                em_uso = caminhao_em_uso(cam.placa)
                col1, col2, col3 = st.columns([3, 2, 1])
                placa_edit = col1.text_input("Placa", value=cam.placa, key=f"placa_cam_{cam.id}", disabled=em_uso)
                modelo_edit = col2.text_input("Modelo", value=cam.modelo or "", key=f"modelo_cam_{cam.id}", disabled=em_uso)
                ano_edit = col2.number_input(
                    "Ano",
                    min_value=1900,
                    max_value=2100,
                    step=1,
                    value=cam.ano or 2000,
                    key=f"ano_cam_{cam.id}",
                    disabled=em_uso,
                )
                novo_status = not cam.ativo
                label_botao = "Excluir" if cam.ativo else "Restaurar"
                if col3.button(label_botao, key=f"toggle_cam_{cam.id}", disabled=em_uso):
                    alterar_status_caminhao(cam.id, novo_status)
                    st.rerun()
                if col3.button("Apagar", key=f"del_cam_{cam.id}", disabled=em_uso):
                    with get_session() as session:
                        obj = session.query(Caminhao).get(cam.id)
                        if obj:
                            session.delete(obj)
                    st.rerun()
                if em_uso:
                    st.info("Caminhao em uso em coletas. Edicao e exclusao bloqueadas.")
                if st.button("Salvar edicao", key=f"save_cam_{cam.id}", disabled=em_uso):
                    with get_session() as session:
                        obj = session.query(Caminhao).get(cam.id)
                        if obj:
                            obj.placa = placa_edit.strip().upper()
                            obj.modelo = modelo_edit.strip() or None
                            obj.ano = int(ano_edit) if ano_edit else None
                    st.success("Caminhao atualizado.")
                    st.rerun()
    else:
        st.info("Nenhum caminhao cadastrado.")

    st.subheader("Cadastrar novo caminhao")
    with st.form("form_caminhao", clear_on_submit=True):
        placa = st.text_input("Placa*", max_chars=10)
        modelo = st.text_input("Modelo (opcional)", max_chars=100)
        ano = st.number_input("Ano (opcional)", min_value=1900, max_value=2100, step=1, format="%d")
        enviado = st.form_submit_button("Salvar caminhao")

        if enviado:
            if not placa.strip():
                st.error("Placa e obrigatoria.")
            else:
                try:
                    criar_caminhao(placa, modelo=modelo, ano=int(ano) if ano else None)
                    st.success("Caminhao salvo com sucesso.")
                except SQLAlchemyError as e:
                    st.error(f"Erro ao salvar caminhao: {e}")

    st.subheader("Caminhoes excluidos")
    caminhoes_inativos = listar_caminhoes(ativos_apenas=False)
    inativos = [c for c in caminhoes_inativos if not c.ativo]
    if inativos:
        for cam in inativos:
            col1, col2 = st.columns([4, 1])
            col1.write(f"{cam.placa} - {cam.modelo or '-'} ({cam.ano or '-'})")
            if col2.button("Restaurar", key=f"rest_cam_{cam.id}"):
                alterar_status_caminhao(cam.id, True)
                st.rerun()
    else:
        st.info("Nenhum caminhao excluido.")


def exibir_nova_coleta():
    st.header("Nova Coleta")
    motoristas = listar_motoristas(ativos_apenas=True)
    if not motoristas:
        st.warning("Cadastre um motorista ativo antes de criar uma coleta.")
        return
    caminhoes = listar_caminhoes(ativos_apenas=True)
    if not caminhoes:
        st.warning("Cadastre um caminhao ativo antes de criar uma coleta.")
        return

    st.session_state.setdefault("nova_protocolo", None)
    st.session_state.setdefault("nova_protocolo_auto", False)

    with st.form("form_coleta"):
        cliente = st.text_input("Cliente*", max_chars=200)
        local_coleta = st.text_input("Local de coleta*", max_chars=200)
        local_entrega = st.text_input("Local de entrega (opcional)", max_chars=200)
        data_combinada = st.date_input("Data combinada*", value=datetime.date.today())
        prazo = st.date_input("Prazo*", value=datetime.date.today())

        motorista_nomes = [f"{m.nome} (#{m.id})" for m in motoristas]
        motorista_escolhido = st.selectbox("Motorista*", motorista_nomes)
        motorista_id = motoristas[motorista_nomes.index(motorista_escolhido)].id
        placas_opts = [c.placa for c in caminhoes]
        placa_veiculo = st.selectbox("Placa do veiculo*", placas_opts)
        valor = st.number_input("Valor (opcional)", min_value=0.0, step=0.01, format="%.2f")
        observacoes = st.text_area("Material / descricao da carga*", max_chars=1000)

        enviado = st.form_submit_button("Salvar coleta")

        if enviado:
            campos_obrigatorios = [
                cliente.strip(),
                local_coleta.strip(),
                data_combinada,
                prazo,
                motorista_id,
                observacoes.strip(),
                placa_veiculo,
            ]
            if not all(campos_obrigatorios):
                st.error("Preencha todos os campos obrigatorios.")
            else:
                dados = {
                    "cliente": cliente.strip(),
                    "local_coleta": local_coleta.strip(),
                    "local_entrega": local_entrega.strip() or None,
                    "data_combinada": data_combinada,
                    "prazo": prazo,
                    "motorista_id": motorista_id,
                    "placa_veiculo": placa_veiculo.strip(),
                    "valor": Decimal(valor) if valor else None,
                    "status": "PENDENTE",
                    "observacoes": observacoes.strip(),
                }
                try:
                    novo_id = criar_coleta(dados)
                    st.success(f"Coleta criada com sucesso. ID: {novo_id}")

                    coleta = obter_coleta_por_id(novo_id)
                    if coleta:
                        obs_auto = "Protocolo gerado automaticamente no cadastro."
                        protocolo_txt = montar_protocolo_txt(coleta, obs_auto)
                        pdf_bytes = gerar_pdf_protocolo(coleta, obs_auto)
                        st.session_state["nova_protocolo"] = {
                            "id": novo_id,
                            "txt": protocolo_txt,
                            "pdf": pdf_bytes,
                            "file_pdf": f"protocolo_coleta_{novo_id}.pdf",
                            "file_txt": f"protocolo_coleta_{novo_id}.txt",
                        }
                        st.session_state["nova_protocolo_auto"] = bool(pdf_bytes)
                    else:
                        st.warning("Nao foi possivel carregar a coleta criada para gerar o protocolo.")
                except SQLAlchemyError as e:
                    st.error(f"Erro ao salvar coleta: {e}")

    proto = st.session_state.get("nova_protocolo")
    if proto:
        st.markdown("---")
        st.subheader("Protocolo gerado")
        st.download_button(
            "Baixar protocolo (TXT)",
            data=proto["txt"],
            file_name=proto["file_txt"],
            mime="text/plain",
            key=f"prot_txt_nova_{proto['id']}",
        )
        if proto.get("pdf"):
            st.download_button(
                "Baixar protocolo (PDF)",
                data=proto["pdf"],
                file_name=proto["file_pdf"],
                mime="application/pdf",
                key=f"prot_pdf_nova_{proto['id']}",
            )
            if st.session_state.get("nova_protocolo_auto"):
                b64_pdf = base64.b64encode(proto["pdf"]).decode("utf-8").replace("\\n", "")
                components.html(
                    f"""
                    <script>
                    (function() {{
                        const link = document.createElement('a');
                        link.href = 'data:application/pdf;base64,{b64_pdf}';
                        link.download = '{proto["file_pdf"]}';
                        document.body.appendChild(link);
                        link.click();
                        document.body.removeChild(link);
                    }})();
                    </script>
                    """,
                    height=0,
                    width=0,
                )
                st.session_state["nova_protocolo_auto"] = False
        else:
            st.warning("Para gerar o PDF automaticamente, instale a dependencia: pip install reportlab")


def _format_indicador(coleta):
    hoje = datetime.date.today()
    if coleta.status == "PENDENTE":
        if coleta.prazo < hoje:
            return "ATRASADA"
        if coleta.prazo == hoje:
            return "PRAZO HOJE"
    if coleta.status == "CONCLUIDA":
        return "Concluida"
    if coleta.status == "CANCELADA":
        return "Cancelada"
    return ""


def exibir_coletas():
    st.header("Coletas")

    resumo = resumo_coletas()
    col_k1, col_k2, col_k3, col_k4 = st.columns(4)
    col_k1.metric("Total", resumo["total"])
    col_k2.metric("Pendentes", resumo["pendente"])
    col_k3.metric("Concluidas", resumo["concluida"])
    col_k4.metric("Canceladas", resumo["cancelada"])

    st.markdown("---")
    col_del_all1, col_del_all2, col_del_all3 = st.columns([2, 2, 1])
    with col_del_all1:
        confirma_tudo = st.checkbox("Confirmo apagar todas as coletas (pendentes/concluidas/canceladas)", key="del_all_coletas")
    with col_del_all2:
        texto_confirmacao = st.text_input('Digite "APAGAR" para confirmar', key="txt_del_all_coletas")
    with col_del_all3:
        if st.button("Apagar todas as coletas", key="btn_del_all_coletas"):
            if not confirma_tudo or texto_confirmacao != "APAGAR":
                st.error('Marque a confirmacao e digite "APAGAR" para apagar todas as coletas.')
            else:
                qtd = remover_todas_coletas()
                st.success(f"{qtd} coletas removidas.")
                st.rerun()

    status_map = {
        "Todas": None,
        "Pendentes": "PENDENTE",
        "Concluidas": "CONCLUIDA",
        "Canceladas": "CANCELADA",
    }

    motoristas_todos = listar_motoristas(ativos_apenas=False)
    motoristas_options = ["Todos"] + [f"{m.nome} (#{m.id})" for m in motoristas_todos]

    col1, col2 = st.columns(2)
    filtro_status = col1.selectbox("Status", list(status_map.keys()), index=1)  # padrao: Pendentes
    filtro_motorista_txt = col2.selectbox("Motorista", motoristas_options)

    col_cb, col_pi, col_pf = st.columns([1, 1, 1])
    usar_prazo = col_cb.checkbox("Filtrar por prazo")
    prazo_inicio = prazo_fim = None
    if usar_prazo:
        prazo_inicio = col_pi.date_input("Prazo inicial", value=datetime.date.today())
        prazo_fim = col_pf.date_input("Prazo final", value=datetime.date.today())

    motorista_id = None
    if filtro_motorista_txt != "Todos":
        motorista_id = int(filtro_motorista_txt.split("#")[-1].strip(")"))

    coletas = listar_coletas(
        status=status_map[filtro_status],
        motorista_id=motorista_id,
        prazo_inicio=prazo_inicio,
        prazo_fim=prazo_fim,
    )

    termo_busca = st.text_input("Buscar (cliente / material / local)", "")
    if termo_busca:
        termo = termo_busca.lower().strip()
        coletas = [
            c
            for c in coletas
            if termo in (c.cliente or "").lower()
            or termo in (c.observacoes or "").lower()
            or termo in (c.local_coleta or "").lower()
            or termo in (c.local_entrega or "").lower()
        ]

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

        try:
            import pandas as pd

            csv = pd.DataFrame(dados).to_csv(index=False).encode("utf-8")
            st.download_button(
                "Baixar resultado (CSV)",
                data=csv,
                file_name="coletas_filtradas.csv",
                mime="text/csv",
            )
        except Exception:
            pass

        opcoes = [f"{c.id} - {c.cliente}" for c in coletas]
        escolha = st.selectbox("Selecione uma coleta para ver detalhes", options=opcoes)
        coleta_id = int(escolha.split(" - ")[0])
        coleta = next((c for c in coletas if c.id == coleta_id), None)
        if coleta:
            exibir_detalhe_coleta(coleta)
    else:
        st.info("Nenhuma coleta encontrada com os filtros selecionados.")
def tamanho_concluidas_bytes():
    """Retorna tamanho aproximado em bytes das coletas concluidas (comprovantes + pequeno overhead)."""
    total = 0
    coletas = listar_coletas(status="CONCLUIDA")
    for c in coletas:
        if c.comprovante_foto:
            total += len(c.comprovante_foto)
        # adiciona um pequeno overhead por registro (campos de texto)
        total += 2048
    return total

    motoristas_todos = listar_motoristas(ativos_apenas=False)
    motoristas_options = ["Todos"] + [f"{m.nome} (#{m.id})" for m in motoristas_todos]

    col1, col2 = st.columns(2)
    filtro_status = col1.selectbox("Status", list(status_map.keys()), index=1)  # padrao: Pendentes
    filtro_motorista_txt = col2.selectbox("Motorista", motoristas_options)

    col_cb, col_pi, col_pf = st.columns([1, 1, 1])
    usar_prazo = col_cb.checkbox("Filtrar por prazo")
    prazo_inicio = prazo_fim = None
    if usar_prazo:
        prazo_inicio = col_pi.date_input("Prazo inicial", value=datetime.date.today())
        prazo_fim = col_pf.date_input("Prazo final", value=datetime.date.today())

    motorista_id = None
    if filtro_motorista_txt != "Todos":
        motorista_id = int(filtro_motorista_txt.split("#")[-1].strip(")"))

    coletas = listar_coletas(
        status=status_map[filtro_status],
        motorista_id=motorista_id,
        prazo_inicio=prazo_inicio,
        prazo_fim=prazo_fim,
    )

    termo_busca = st.text_input("Buscar (cliente / material / local)", "")
    if termo_busca:
        termo = termo_busca.lower().strip()
        coletas = [
            c
            for c in coletas
            if termo in (c.cliente or "").lower()
            or termo in (c.observacoes or "").lower()
            or termo in (c.local_coleta or "").lower()
            or termo in (c.local_entrega or "").lower()
        ]

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

        if coletas:
            try:
                import pandas as pd

                csv = pd.DataFrame(dados).to_csv(index=False).encode("utf-8")
                st.download_button(
                    "Baixar resultado (CSV)",
                    data=csv,
                    file_name="coletas_filtradas.csv",
                    mime="text/csv",
                )
            except Exception:
                pass

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
        **Material/Obs:** {coleta.observacoes or '-'}  
        **Criado em:** {fmt_dt(coleta.criado_em)}  
        **Atualizado em:** {fmt_dt(coleta.atualizado_em)}  
        **Concluido em:** {fmt_dt(coleta.concluido_em)}  
        """
    )

    if coleta.comprovante_foto:
        st.image(
            io.BytesIO(coleta.comprovante_foto),
            caption=coleta.comprovante_foto_nome or "Comprovante",
            width=600,
        )

    st.markdown("---")
    st.subheader("Protocolo")
    observacao_padrao = "Protocolo gerado pelo sistema (reenvie para motorista/cliente)."
    protocolo_txt = montar_protocolo_txt(coleta, observacao_padrao)
    st.download_button(
        "Baixar protocolo (TXT)",
        data=protocolo_txt,
        file_name=f"protocolo_coleta_{coleta.id}.txt",
        mime="text/plain",
        key=f"prot_txt_{coleta.id}",
    )
    pdf_bytes = gerar_pdf_protocolo(coleta, observacao_padrao)
    if pdf_bytes:
        st.download_button(
            "Baixar protocolo (PDF)",
            data=pdf_bytes,
            file_name=f"protocolo_coleta_{coleta.id}.pdf",
            mime="application/pdf",
            key=f"prot_pdf_{coleta.id}",
        )
    else:
        st.warning("Para gerar PDF, instale a dependencia: pip install reportlab")

    # Status rapido (sem editar demais campos)
    if coleta.status not in ("CONCLUIDA", "CANCELADA"):
        st.markdown("---")
        st.subheader("Atualizar status")
        novo_status_status = st.selectbox(
            "Status",
            options=STATUS_OPCOES,
            index=STATUS_OPCOES.index(coleta.status),
            key=f"status_only_{coleta.id}",
        )
        foto_status = None
        if novo_status_status == "CONCLUIDA":
            foto_status = st.file_uploader(
                "Foto de comprovante (obrigatorio ao concluir)",
                type=["jpg", "jpeg", "png"],
                key=f"foto_status_{coleta.id}",
            )
        confirmar_status = st.checkbox(
            "Confirmo a alteracao de status desta coleta",
            value=False,
            key=f"confirma_status_{coleta.id}",
        )
        if st.button("Salvar status", key=f"btn_status_{coleta.id}"):
            if not confirmar_status:
                st.error("Marque a confirmacao antes de alterar o status.")
            else:
                sucesso, mensagem = atualizar_coleta(coleta.id, {}, novo_status=novo_status_status, foto=foto_status)
                if sucesso:
                    st.success(mensagem)
                    st.rerun()
                else:
                    st.error(mensagem)

    st.markdown("---")
    st.subheader("Editar coleta")
    bloqueada = coleta.status in ("CONCLUIDA", "CANCELADA")
    motoristas_ativos = listar_motoristas(ativos_apenas=True)

    if bloqueada:
        st.info("Coleta concluida ou cancelada. Edicao desabilitada.")
    elif not motoristas_ativos:
        st.warning("Nenhum motorista ativo para edicao.")
    else:
        habilitar_edicao = st.checkbox(
            "Habilitar edicao desta coleta (pendente/em rota)",
            value=False,
            key=f"habilita_edicao_{coleta.id}",
        )

        if not habilitar_edicao:
            st.warning("Marque a caixa acima para liberar o formulario de edicao.")
        else:
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

                caminhoes_ativos = listar_caminhoes(ativos_apenas=True)
                placas_opts = sorted(
                    set([c.placa for c in caminhoes_ativos] + ([coleta.placa_veiculo] if coleta.placa_veiculo else []))
                )
                placa_idx = placas_opts.index(coleta.placa_veiculo) if coleta.placa_veiculo in placas_opts else 0
                placa_veiculo = st.selectbox("Placa do veiculo*", placas_opts, index=placa_idx)
                valor = st.number_input(
                    "Valor (opcional)",
                    min_value=0.0,
                    step=0.01,
                    format="%.2f",
                    value=float(coleta.valor) if coleta.valor is not None else 0.0,
                )
                observacoes = st.text_area("Material / observacoes", value=coleta.observacoes or "", max_chars=1000)

                novo_status = st.selectbox("Status", options=STATUS_OPCOES, index=STATUS_OPCOES.index(coleta.status))
                foto = st.file_uploader(
                    "Foto de comprovante da carga (obrigatorio ao concluir)",
                    type=["jpg", "jpeg", "png"],
                    key=f"foto_{coleta.id}",
                )

                salvar = st.form_submit_button("Salvar alteracoes")

                if salvar:
                    if not cliente.strip() or not local_coleta.strip() or not data_combinada or not prazo or not motorista_id:
                        st.error("Preencha todos os campos obrigatorios.")
                    else:
                        campos = {
                            "cliente": cliente.strip(),
                            "local_coleta": local_coleta.strip(),
                            "local_entrega": local_entrega.strip() or None,
                            "data_combinada": data_combinada,
                            "prazo": prazo,
                            "motorista_id": motorista_id,
                            "placa_veiculo": placa_veiculo.strip(),
                            "valor": Decimal(valor) if valor else None,
                            "observacoes": observacoes.strip() or None,
                        }

                        sucesso, mensagem = atualizar_coleta(coleta.id, campos, novo_status=novo_status, foto=foto)
                        if sucesso:
                            st.success(mensagem)
                            st.rerun()
                        else:
                            st.error(mensagem)

    st.markdown("---")
    st.subheader("Excluir coleta")
    col_del1, col_del2 = st.columns([3, 1])
    with col_del1:
        confirmar = st.checkbox("Confirmo que desejo excluir esta coleta (acao irreversivel)", key=f"conf_{coleta.id}")
    with col_del2:
        if st.button("Excluir", key=f"del_{coleta.id}"):
            if not confirmar:
                st.error("Marque a confirmacao para excluir.")
            else:
                ok, msg = remover_coleta(coleta.id)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)


def exibir_coletas_excluidas():
    st.header("Coletas excluidas")
    with get_session() as session:
        excluidas = session.query(ColetaExcluida).order_by(ColetaExcluida.deletado_em.desc()).all()
    if not excluidas:
        st.info("Nenhuma coleta excluida registrada.")
        return

    dados = []
    for c in excluidas:
        dados.append(
            {
                "ID original": c.coleta_id,
                "Cliente": c.cliente,
                "Status": c.status,
                "Motorista": c.motorista_nome or "-",
                "Placa": c.placa_veiculo or "-",
                "Local coleta": c.local_coleta,
                "Local entrega": c.local_entrega or "-",
                "Data combinada": c.data_combinada.strftime('%d/%m/%Y') if c.data_combinada else '-',
                "Prazo": c.prazo.strftime('%d/%m/%Y') if c.prazo else '-',
                "Criado em": fmt_dt(c.criado_em),
                "Concluido em": fmt_dt(c.concluido_em),
                "Excluido em": fmt_dt(c.deletado_em),
                "Material/Obs": c.observacoes or '-',
            }
        )

    try:
        import pandas as pd
        st.dataframe(pd.DataFrame(dados), use_container_width=True)
    except Exception:
        st.table(dados)

    st.markdown("---")
    col_del_all = st.columns([1, 3, 1])
    with col_del_all[0]:
        confirmar_apagar = st.checkbox("Confirmo apagar todas excluidas", value=False, key="conf_del_excluidas")
    with col_del_all[1]:
        txt_conf_exc = st.text_input('Digite "APAGAR" para confirmar', key="txt_del_excluidas")
    with col_del_all[2]:
        if st.button("Apagar excluidas"):
            if not confirmar_apagar or txt_conf_exc != "APAGAR":
                st.error('Marque a confirmacao e digite "APAGAR" para apagar.')
            else:
                with get_session() as session:
                    session.query(ColetaExcluida).delete()
                st.success("Todas as coletas excluidas foram removidas.")
                st.rerun()


def exibir_concluidas():
    st.header("Coletas concluidas")
    coletas = listar_coletas(status="CONCLUIDA")

    tamanho = tamanho_concluidas_bytes()
    if tamanho >= BACKUP_THRESHOLD_BYTES:
        st.warning(
            f"Backup recomendado: dados de concluidas somam ~{tamanho / (1024*1024):.1f} MB (limite 360 MB).",
            icon="⚠️",
        )
    col_zip, col_del = st.columns([2, 1])
    with col_zip:
        if st.button("Baixar ZIP (PDFs + comprovantes + resumos)"):
            ok, resultado = gerar_zip_concluidas()
            if ok:
                st.download_button(
                    "Baixar agora",
                    data=resultado,
                    file_name="backup_coletas_concluidas.zip",
                    mime="application/zip",
                    key="zip_concluidas",
                )
            else:
                st.warning(resultado)
    with col_del:
        confirmar_apagar = st.checkbox("Confirmo apagar todas as concluidas", value=False, key="conf_del_concluidas")
        txt_conf = st.text_input('Digite "APAGAR" para confirmar', key="txt_del_concluidas")
        if st.button("Apagar concluidas"):
            if not confirmar_apagar or txt_conf != "APAGAR":
                st.error('Marque a confirmacao e digite "APAGAR" para apagar.')
            else:
                ids = [c.id for c in coletas]
                sucesso_total = True
                for cid in ids:
                    ok, _ = remover_coleta(cid)
                    if not ok:
                        sucesso_total = False
                if sucesso_total:
                    st.success("Coletas concluidas removidas (backup se necessario foi feito separadamente).")
                else:
                    st.warning("Algumas coletas nao puderam ser removidas.")
                st.rerun()
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
        **Material/Obs:** {coleta.observacoes or '-'}  
        **Criado em:** {fmt_dt(coleta.criado_em)}  
        **Concluido em:** {fmt_dt(coleta.concluido_em)}  
        """
    )

    if coleta.comprovante_foto:
        st.image(
            io.BytesIO(coleta.comprovante_foto),
            caption=coleta.comprovante_foto_nome or "Comprovante",
            width=600,
        )

    st.markdown("---")
    st.subheader("Protocolo para impressao")
    observacao_extra = "Protocolo gerado pelo sistema (reenvie para motorista/cliente)."
    if st.button("Gerar protocolo"):
        protocolo = f"""
PROTOCOLO DE RECEBIMENTO - COLETA #{coleta.id}
Observacao: Conferir a carga e registrar divergencias antes da assinatura.
Cliente: {coleta.cliente}
Motorista: {coleta.motorista.nome if coleta.motorista else '-'}
Placa do veiculo: {coleta.placa_veiculo or '-'}
Local de coleta: {coleta.local_coleta}
Local de entrega: {coleta.local_entrega or '-'}
Material/Carga: {coleta.observacoes or '-'}
Data de emissao: {datetime.date.today().strftime('%d/%m/%Y')}
Observacao adicional: {observacao_extra or '-'}
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
            b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8").replace("\n", "")
            if st.button("Imprimir protocolo agora"):
                components.html(
                    f"""
                    <script>
                    (function() {{
                        const b64 = "{b64_pdf}";
                        const src = "data:application/pdf;base64," + b64;
                        const iframe = document.createElement('iframe');
                        iframe.style.position = 'fixed';
                        iframe.style.right = '0';
                        iframe.style.bottom = '0';
                        iframe.style.width = '0';
                        iframe.style.height = '0';
                        iframe.style.border = '0';
                        iframe.src = src;
                        document.body.appendChild(iframe);
                        iframe.onload = () => {{
                            try {{
                                iframe.contentWindow.focus();
                                iframe.contentWindow.print();
                            }} catch (e) {{
                                alert("Nao foi possivel abrir o PDF. Baixe o arquivo e imprima manualmente.");
                            }}
                        }};
                    }})();
                    </script>
                    """,
                    height=0,
                    width=0,
                )
        else:
            st.warning("Para gerar PDF, instale a dependencia: pip install reportlab")

        st.info("Para imprimir, baixe o PDF (ou TXT) e envie para a impressora.")

    st.markdown("---")
    st.subheader("Excluir coleta")
    col_del1, col_del2 = st.columns([3, 1])
    with col_del1:
        confirmar = st.checkbox(
            "Confirmo que desejo excluir esta coleta (acao irreversivel)",
            key=f"conf_concluida_{coleta.id}",
        )
    with col_del2:
        if st.button("Excluir", key=f"del_concluida_{coleta.id}"):
            if not confirmar:
                st.error("Marque a confirmacao para excluir.")
            else:
                ok, msg = remover_coleta(coleta.id)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)


def main():
    get_engine()
    garantir_admin_padrao()
    if "usuario" not in st.session_state:
        st.session_state["usuario"] = None
    if st.session_state["usuario"] is None:
        st.title("Login")
        with st.form("form_login"):
            usr = st.text_input("Usuario")
            pwd = st.text_input("Senha", type="password")
            entrar = st.form_submit_button("Entrar")
            if entrar:
                if autenticar(usr, pwd):
                    st.session_state["usuario"] = usr.strip().lower()
                    st.success("Login realizado.")
                    st.rerun()
                else:
                    st.error("Usuario ou senha invalidos.")
        st.stop()

    st.sidebar.write(f"Logado como: {st.session_state['usuario']}")
    if st.sidebar.button("Logout"):
        st.session_state["usuario"] = None
        st.rerun()

    logo = carregar_logo()
    if logo:
        st.image(logo, width=90)
    st.title("Controle de Coletas")

    pagina = st.sidebar.radio(
        "Navegacao",
        ("Coletas", "Concluidas", "Nova Coleta", "Motoristas", "Caminhoes", "Coletas excluidas"),
    )

    if pagina == "Motoristas":
        exibir_motoristas()
    elif pagina == "Concluidas":
        exibir_concluidas()
    elif pagina == "Nova Coleta":
        exibir_nova_coleta()
    elif pagina == "Caminhoes":
        exibir_caminhoes()
    elif pagina == "Coletas excluidas":
        exibir_coletas_excluidas()
    else:
        exibir_coletas()


if __name__ == "__main__":
    main()
