import streamlit as st
import pandas as pd
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException
import requests
from bs4 import BeautifulSoup
import zipfile
import io

st.set_page_config(page_title="Radar Parlamentar & Gastos", layout="wide")
st.sidebar.header("Filtros de Pesquisa")
ano_selecionado = st.sidebar.selectbox("Ano:", [2026, 2025, 2024, 2023, 2022], index=0)

# --- FUNÇÃO 1: BAIXAR E FILTRAR DADOS FINANCEIROS ---
@st.cache_data
def carregar_dados_gastos(ano: int, uf: str):

    url_csv = f"http://www.camara.leg.br/cotas/Ano-{ano}.csv.zip"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        )
    }

    ultimo_erro = None

    # Tenta baixar até 3 vezes
    for tentativa in range(1, 4):

        try:
            st.write(
                f"Baixando dados de {ano}... "
                f"(tentativa {tentativa}/3)"
            )

            response = requests.get(
                url_csv,
                headers=headers,
                timeout=120
            )

            response.raise_for_status()

            # Verifica se realmente recebemos conteúdo
            if not response.content:
                raise Exception("O servidor retornou um arquivo vazio.")

            # Verifica se é um ZIP válido
            arquivo_zip = io.BytesIO(response.content)

            with zipfile.ZipFile(arquivo_zip) as z:

                arquivos = z.namelist()

                if not arquivos:
                    raise Exception("O ZIP não contém arquivos.")

                # Normalmente existe apenas um CSV
                nome_csv = arquivos[0]

                with z.open(nome_csv) as arquivo_csv:

                    df = pd.read_csv(
                        arquivo_csv,
                        sep=";",
                        low_memory=False
                    )

            break

        except Exception as e:

            ultimo_erro = e

            st.warning(
                f"Falha no download (tentativa {tentativa}/3): {e}"
            )

            if tentativa < 3:
                time.sleep(3)

    else:

        raise Exception(
            f"Não foi possível baixar os dados da Câmara após 3 tentativas. "
            f"Erro: {ultimo_erro}"
        )

    # Filtra UF
    if uf != "TODOS":
        df = df[df["sgUF"] == uf].copy()

    # Agrupa gastos
    gastos = (
        df.groupby(
            ["ideCadastro", "txNomeParlamentar", "sgUF"]
        )["vlrLiquido"]
        .sum()
        .reset_index()
    )

    gastos = gastos.sort_values(
        by="vlrLiquido",
        ascending=False
    )

    return gastos

# --- FUNÇÃO 2: SELENIUM PARA ENRIQUECER OS DADOS ---
def extrair_detalhes_selenium(id_deputado: int):
    url_perfil = f"https://www.camara.leg.br/deputados/{id_deputado}"
    
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1920,1080")
    
    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 8)
    
    # Inicializa o dicionário com valores padrão caso algo falhe
    dados = {
        "foto": "https://www.camara.leg.br/tema/assets/images/foto-deputado-sem-foto.png",
        "partido": "Não identificado",
        "email": "Não identificado",
        "url": url_perfil,
        "salario": "Não informado",
        "imovel_moradia": "Não informado",
        "equipe_gabinete": "0",
        "presenca_plenario": {"dias": "0", "presencas": "0", "ausencias_justificadas": "0", "ausencias_nao_justificadas": "0"},
        "presenca_comissao": {"dias": "0", "presencas": "0", "ausencias_justificadas": "0", "ausencias_nao_justificadas": "0"}
    }
    
    try:
        driver.get(url_perfil)
        
        # 1. Informações Básicas (Foto, Partido, Email)
        try:
            elem_foto = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".foto-deputado img")))
            dados["foto"] = elem_foto.get_attribute("src")
        except: pass

        try:
            elem_partido = driver.find_element(By.XPATH, "//ul[@class='informacoes-deputado']//li[span[contains(text(), 'Partido:')]]")
            dados["partido"] = elem_partido.text.replace("Partido:", "").strip()
        except: pass

        try:
            elem_email = driver.find_element(By.CSS_SELECTOR, "a[href^='mailto:']")
            dados["email"] = elem_email.text.strip()
        except: pass

        # 2. Salário
        try:
            # Procura pelo link de remuneração para pegar o valor exibido
            elem_salario = driver.find_element(By.XPATH, "//a[contains(@href, 'remuneracao') and contains(text(), 'R$')]")
            dados["salario"] = elem_salario.text.strip()
        except: pass
        
        # 3. Imóvel Funcional / Auxílio-Moradia
        try:
            # CORREÇÃO: Busca por qualquer tag <li> que contenha as palavras-chave no texto.
            beneficios = driver.find_elements(By.XPATH, "//li[contains(., 'Imóvel funcional') or contains(., 'Auxílio-moradia')]")
            info_moradia = [b.text.strip() for b in beneficios]
            

            
            if info_moradia:
                dados["imovel_moradia"] = " | ".join(info_moradia)
                
        except: pass

        # 4. Tamanho do Gabinete (Secretários Parlamentares)
        try:
            # CORREÇÃO: Em vez de procurar pela classe ".badge" (que pode mudar), procuramos 
            # o link de pessoal e filtramos apenas os números contidos na string inteira.
            elem_equipe = driver.find_element(By.XPATH, "//a[contains(@href, 'pessoal-gabinete')]")
            numeros = elem_equipe.text
            
            if numeros:
                dados["equipe_gabinete"] = numeros
        except: pass

        # 5. Presenças e Faltas (Plenário e Comissões)
        try:

            import re
            import requests
            from bs4 import BeautifulSoup

            # ------------------------------------------------------
            # A página de presença é obtida diretamente com
            # requests + BeautifulSoup.
            #
            # Não usamos o Selenium para esta parte porque os
            # valores já estão presentes no HTML retornado pela
            # página.
            # ------------------------------------------------------

            url_presencas = (
                f"https://www.camara.leg.br/"
                f"deputados/{id_deputado}?ano={ano_selecionado}"
            )

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/153.0.0.0 Safari/537.36"
                )
            }

            response = requests.get(
                url_presencas,
                headers=headers,
                timeout=30
            )

            response.raise_for_status()

            soup = BeautifulSoup(
                response.text,
                "lxml"
            )

            # ------------------------------------------------------
            # Localiza as duas seções:
            #
            # 1 - Presença em Plenário
            # 2 - Presença em Comissões
            # ------------------------------------------------------

            secoes = soup.select(
                "div.presencas__content > section.presencas__section"
            )

            for secao in secoes:

                # --------------------------------------------------
                # Título da seção
                # --------------------------------------------------

                titulo_elemento = secao.select_one(
                    ".presencas__section-heading"
                )

                if not titulo_elemento:
                    continue

                titulo = titulo_elemento.get_text(
                    " ",
                    strip=True
                )

                # --------------------------------------------------
                # Valores
                #
                # O HTML possui:
                #
                # <span class="presencas__qtd">
                #     69 dias
                # </span>
                #
                # <span class="presencas__qtd">
                #     0 dias
                # </span>
                #
                # <span class="presencas__qtd">
                #     0 dias
                # </span>
                # --------------------------------------------------

                valores = secao.select(
                    ".presencas__qtd"
                )

                if len(valores) < 3:
                    continue

                numeros = []

                for valor in valores[:3]:

                    texto = valor.get_text(
                        " ",
                        strip=True
                    )

                    numero = re.search(
                        r"\d+",
                        texto
                    )

                    if numero:
                        numeros.append(
                            numero.group()
                        )

                if len(numeros) != 3:
                    continue

                # --------------------------------------------------
                # Monta os dados
                # --------------------------------------------------

                dados_assiduidade = {
                    "dias": "0",
                    "presencas": numeros[0],
                    "ausencias_justificadas": numeros[1],
                    "ausencias_nao_justificadas": numeros[2]
                }

                # --------------------------------------------------
                # PLENÁRIO
                # --------------------------------------------------

                if "Plenário" in titulo:

                    dados["presenca_plenario"] = (
                        dados_assiduidade
                    )

                # --------------------------------------------------
                # COMISSÕES
                # --------------------------------------------------

                elif "Comissões" in titulo:

                    dados["presenca_comissao"] = (
                        dados_assiduidade
                    )

        except Exception as e:

            print(
                f"Erro ao extrair presenças: {e}"
            )    
            
    finally:
        driver.quit()
        
    return dados

# --- INTERFACE STREAMLIT ---
st.title("🏛️ Radar de Gastos da Cota Parlamentar")
st.markdown("Busque deputados específicos ou veja o ranking dos maiores gastos estaduais.")

#ano_selecionado = st.sidebar.selectbox("Ano:", [2026, 2025, 2024, 2023, 2022], index=0)

ufs_brasil = [
    "TODOS", "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA",
    "MG", "MS", "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN",
    "RO", "RR", "RS", "SC", "SE", "SP", "TO"
]
uf_selecionada = st.sidebar.selectbox("Estado (UF):", ufs_brasil, index=ufs_brasil.index("MG"))

# st.sidebar.divider()
top_n = st.sidebar.slider("Quantidade (Ranking):", 1, 15, 6)

if st.sidebar.button("Processar Dados"):
    with st.spinner(f"Baixando e processando base de dados de {ano_selecionado}..."):
        df_ranking = carregar_dados_gastos(ano_selecionado, uf_selecionada)
        
    if df_ranking.empty:
        st.warning("Nenhum dado financeiro encontrado.")
    else:
        top_deputados = df_ranking.head(top_n)
        st.subheader(f"Top {top_n} Maiores Gastos de {uf_selecionada} em {ano_selecionado}")
        
        if top_deputados.empty:
            st.warning("Nenhum parlamentar encontrado com esse nome nos filtros selecionados.")
            st.stop()
        
        colunas_por_linha = 3
        
        for i in range(0, len(top_deputados), colunas_por_linha):
            cols = st.columns(colunas_por_linha)
            chunk = top_deputados.iloc[i:i+colunas_por_linha]
            
            for idx, (_, row) in enumerate(chunk.iterrows()):
                id_dep = int(row["ideCadastro"])
                nome_dep = row["txNomeParlamentar"]
                estado_dep = row["sgUF"]
                total_gasto = row["vlrLiquido"]
                
                with cols[idx]:
                    with st.spinner(f"Acessando perfil de {nome_dep}..."):
                        info = extrair_detalhes_selenium(id_dep)
                    
                    with st.container(border=True):
                        col_img, col_info = st.columns([1, 2])
                        with col_img:
                            st.image(info["foto"], width='content') 
                        with col_info:
                            st.markdown(f"#### {nome_dep}")
                            partido_exibicao = info['partido'] if info['partido'] != "Não identificado" else "Sem Partido"
                            st.caption(f"*{partido_exibicao}*")
                            st.metric("Cota Parlamentar", f"R$ {total_gasto:,.2f}".replace(",", "v").replace(".", ",").replace("v", "."))

                        # ... (código existente da foto e nome) ...

                        st.divider()
                        st.markdown("*📊 Comparativo de Gastos (Cota)*")
                        st.metric(
                                 label="Valor gasto", 
                                 value=f"R$ {total_gasto:,.2f}".replace(",", "v").replace(".", ",").replace("v", "."),
                                 help="Soma bruta das notas fiscais do ano isolado."
                             )
                        
                        
                        # --- EXIBIÇÃO DOS NOVOS DADOS ---
                        st.divider()
                        st.markdown(f"*💰 Salário:* {info['salario']}")
                        st.markdown(f"*🏠 Faz uso de* {info['imovel_moradia'].split('|')[0].strip()}")
                        st.markdown(f"*🏠 Quanto recebeu de Auxílio Moradia* {info['imovel_moradia'].split('|', 1)[1].strip()}.")
                        st.markdown(f"*👥 Equipe de Gabinete:* {info['equipe_gabinete']}")
                        
                        # Abas para separar Plenário e Comissões
                        aba_plenario, aba_comissao = st.tabs(["🏛️ Plenário", "👥 Comissões"])
                        
                        with aba_plenario:
                            p = info["presenca_plenario"]

                            sessoes_totais = (
                                int(p["presencas"])
                                + int(p["ausencias_justificadas"])
                                + int(p["ausencias_nao_justificadas"])
                            )

                            st.write(
                                f"Sessões: *{sessoes_totais}* | "
                                f"Presenças: *{p['presencas']}*"
                            )

                            st.write(
                                f"Faltas Justificadas: *{p['ausencias_justificadas']}*"
                            )

                            st.write(
                                f"Faltas Não Justificadas: *{p['ausencias_nao_justificadas']}*"
                            )


                        with aba_comissao:
                            c = info["presenca_comissao"]

                            reunioes_totais = (
                                int(c["presencas"])
                                + int(c["ausencias_justificadas"])
                                + int(c["ausencias_nao_justificadas"])
                            )

                            st.write(
                                f"Reuniões: *{reunioes_totais}* | "
                                f"Presenças: *{c['presencas']}*"
                            )

                            st.write(
                                f"Faltas Justificadas: *{c['ausencias_justificadas']}*"
                            )

                            st.write(
                                f"Faltas Não Justificadas: *{c['ausencias_nao_justificadas']}*"
                            )
                        
                        st.divider()                    
                        st.write(f"📧 {info['email']}")
                        st.link_button("Ver Perfil Completo na Câmara", info["url"], width='content')