# Controle de Coletas (Streamlit)

Aplicação Streamlit em Python para controle de coletas com PostgreSQL (Neon). Inclui cadastro de motoristas, criação/edição de coletas, conclusão com foto obrigatória e emissão de protocolo em PDF.

## Pré-requisitos
- Python 3.10+  
- Dependências: `pip install streamlit sqlalchemy psycopg2-binary pandas reportlab`
- Banco PostgreSQL com URL no formato `postgresql://usuario:senha@host/dbname?sslmode=require&channel_binding=require`

## Configuração do banco
1) Crie/obtenha a URL do seu banco (ex.: Neon).  
2) Defina a variável de ambiente `DATABASE_URL` **ou** crie `.streamlit/secrets.toml` copiando do template:
   - Copie `.streamlit/secrets.example.toml` para `.streamlit/secrets.toml`
   - Substitua a URL no arquivo copiado

O arquivo real `.streamlit/secrets.toml` está no `.gitignore` para evitar vazamento de credenciais.

## Rodar local
```bash
streamlit run app.py
```

## Estrutura principal
- `app.py` — app Streamlit completo.
- `.streamlit/secrets.example.toml` — template de credenciais (use para criar o seu `.streamlit/secrets.toml`).
- `.gitignore` — ignora o secrets real e caches.

## Deploy no Streamlit Cloud
1) Faça push para o GitHub.  
2) No Streamlit Cloud, aponte para este repositório.  
3) Defina o segredo `DATABASE_URL` nas “Secrets” do projeto (cole a URL do banco).  
4) Deploy.

## Observação sobre protocolo/assinaturas
O protocolo exige assinatura manual de gestor e motorista. Há opção de gerar PDF com logo (logo-jr.png) e imprimir diretamente.
