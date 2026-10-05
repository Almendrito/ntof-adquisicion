"""
analisis_pulsos.py

Qué hace:
    Abre los archivos .csv que guarda captura_tds684a.py y analiza el pulso
    de señal de cada canal (en estos registros hay un solo pulso, el de rayos X).
    Para cada pulso mide la línea base, el ruido, la amplitud, el ancho a media
    altura (FWHM), los tiempos de subida y bajada, la integral y la carga. Muestra
    cada pulso con sus marcas, compara la forma de varios pulsos superpuestos
    (alineados y normalizados, con su forma media) y grafica cómo cambian las
    mediciones con la diferencia de potencial, la distancia o el número de disparo.

Cómo se ejecuta:
    python analisis_pulsos.py

Qué necesita:
    - Python 3.9 o más nuevo con tkinter.
    - Librerías:  pip install numpy matplotlib
    - Archivos .csv guardados por captura_tds684a.py.

Qué produce:
    - En pantalla: una tabla con las mediciones de cada pulso y tres gráficos.
    - Con «Exportar tabla…»: un .csv con todas las mediciones, una fila por pulso.

Cómo se mide cada pulso (para el análisis completo de la tesis):
    1. Línea base = mediana de todo el registro. El pulso ocupa una parte
       mínima del registro, así que casi no mueve la mediana.
    2. Ruido = 1,4826 × mediana de |V − línea base|. Para ruido gaussiano
       esto es igual a la desviación estándar, pero no lo afecta el pulso.
    3. Polaridad: el pulso es el lado (positivo o negativo) que más se aleja
       de la línea base, salvo que se elija a mano.
    4. Amplitud y tiempo del pico: vértice de una parábola ajustada a la cima
       (los puntos sobre el 90 % del máximo). Así el ruido no infla la amplitud.
    5. Los cruces del 10, 50 y 90 % se buscan caminando desde el pico hacia
       afuera, con interpolación lineal entre muestras. Cada flanco se suaviza
       antes con un promedio móvil del 10 % de su propia duración, para que el
       ruido no adelante los cruces de la cola lenta sin alargar el flanco
       rápido. FWHM = distancia entre los cruces del 50 %.
    6. Subida = tiempo entre el 10 % y el 90 % en el flanco de entrada.
       Bajada = tiempo entre el 90 % y el 10 % en el flanco de salida.
    7. Integral = área del pulso entre sus dos cruces del 10 %, por trapecios.
       Carga = integral / 50 Ω, solo si el canal estaba en 50 Ω.
    8. Saturado: si el pulso llegó a 5 divisiones del centro de la pantalla,
       el digitalizador se saturó y la amplitud real es mayor que la medida.

Herramientas avanzadas usadas (ver los comentarios NOTA en el código):
    - Un hilo (threading) para leer muchos archivos sin congelar la ventana.
    - La clase App hereda de ttk.Frame, igual que en la plantilla de
      interfaces. Heredar significa que App es un contenedor de tkinter con
      todo lo que este ya sabe hacer, más los métodos que le agregamos.
"""

# Para obtener el nombre de cada archivo sin su carpeta
import os
# Para saber si el programa corre en Windows (nitidez de la ventana)
import sys
# Cola segura para pasar los archivos leídos desde el hilo a la ventana
import queue
# Para leer los archivos en un hilo aparte y no congelar la ventana
import threading
# Ventanas, botones, tablas y campos de texto
import tkinter as tk
from tkinter import ttk, font, filedialog, messagebox

# Cálculos sobre los registros completos (mediana, máximo, interpolación)
import numpy as np
# Gráficos dentro de la ventana
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
# Escalas de colores, para distinguir muchos pulsos superpuestos
from matplotlib import colormaps


# ─── Tokens de diseño (de la plantilla de interfaces, no modificar) ────────
COLORS = {
    "bg":        "#F5F7FA",  # fondo de la ventana
    "surface":   "#FFFFFF",  # tarjetas y paneles
    "border":    "#E2E6EB",  # líneas finas
    "text":      "#1C2733",  # texto principal
    "muted":     "#6B7683",  # etiquetas y texto secundario
    "accent":    "#2B5FB0",  # color de acción (un solo acento)
    "accent_fg": "#FFFFFF",
    "ok":        "#2E9E6B",  # correcto
    "warn":      "#C8860B",  # advertencia
    "error":     "#D64545",  # falla
}

# Escala de espaciado en píxeles (múltiplos de 4). Usar siempre estas constantes.
PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 16, 24

# Colores para varias series (de la plantilla de interfaces): uno por canal
COLORES_DE_SERIES = ["#2B5FB0", "#2E9E6B", "#C8860B", "#8452C4", "#D64545"]


# ─── Constantes del análisis ───────────────────────────────────────────────

# Factor que convierte la mediana de las desviaciones absolutas (MAD) en la
# desviación estándar, para ruido gaussiano. Es 1 / 0,6745.
FACTOR_MAD_A_DESVIACION = 1.4826

# Fracciones de la amplitud que definen las mediciones de tiempo
FRACCION_MITAD = 0.5   # para el FWHM
FRACCION_BAJA = 0.1    # 10 %, inicio de la subida y fin de la bajada
FRACCION_ALTA = 0.9    # 90 %, fin de la subida e inicio de la bajada

# Impedancia de entrada del osciloscopio en modo 50 Ω, en ohm.
# Sirve para pasar de integral de voltaje (V·s) a carga (C = V·s / Ω).
RESISTENCIA_DE_ENTRADA_OHM = 50.0

# Altura, en divisiones desde el centro de la pantalla, desde la cual el
# digitalizador del TDS 684A ya está saturado (su límite es ±5,12 div).
DIVISIONES_DE_SATURACION = 5.0

# Bajo esta razón señal/ruido se avisa que el pulso no se distingue bien.
SENAL_RUIDO_MINIMA = 5.0

# Fracción del máximo sobre la cual se considera "la cima" del pulso.
# A esos puntos se les ajusta una parábola para obtener la amplitud y el
# tiempo del pico sin el sesgo del ruido (el máximo de muestras ruidosas
# siempre queda un poco por encima del pico real).
FRACCION_CIMA = 0.9

# Mínimo de puntos en la cima para que el ajuste de la parábola tenga sentido
PUNTOS_MINIMOS_PARABOLA = 5

# Suavizado de cada flanco antes de buscar sus cruces, como fracción de la
# duración de ese flanco (10–90 %). Sin suavizado, una bajada del ruido
# adelanta el cruce en las colas lentas y acorta la bajada medida; con un
# suavizado fijo grande, en cambio, se alarga la subida de los flancos rápidos.
# Con el 10 % de cada flanco, en pulsos simulados con S/R entre 40 y 65, los
# sesgos quedaron bajo el 1 % en amplitud, bajada e integral, bajo el 2 % en
# el FWHM y bajo el 4 % en la subida (donde el ruido solo ya da ±3 a ±11 %).
FRACCION_DEL_FLANCO_PARA_SUAVIZAR = 0.1

# Cuántos anchos (FWHM) se muestran a cada lado del pulso en la vista "Pulso"
ANCHOS_A_CADA_LADO_EN_VISTA = 3.0

# Tramo que se compara en "Comparar formas", en anchos (FWHM) del pulso más
# ancho: antes y después del punto de alineación.
ANCHOS_ANTES_EN_COMPARACION = 2.0
ANCHOS_DESPUES_EN_COMPARACION = 5.0

# Puntos de la grilla común donde se calcula la forma media
PUNTOS_DE_LA_FORMA_MEDIA = 400

# Cada cuánto la ventana revisa si el hilo terminó, en milisegundos
INTERVALO_REVISION_MS = 100

# Segundos en un nanosegundo, para mostrar tiempos en ns
SEGUNDOS_POR_NANOSEGUNDO = 1e-9

# Coulomb en un nanocoulomb, para mostrar la carga en nC
COULOMB_POR_NANOCOULOMB = 1e-9

# Opciones de las listas desplegables
OPCIONES_DE_POLARIDAD = ["Automática", "Negativa", "Positiva"]
OPCIONES_DE_ALINEACION = ["Flanco al 50 %", "Pico"]
OPCIONES_EJE_X = ["Diferencia de potencial (kV)", "Distancia del detector (cm)", "Número de disparo"]
OPCIONES_EJE_Y = ["Amplitud (V)", "Ancho FWHM (ns)", "Subida 10–90 % (ns)", "Bajada 90–10 % (ns)",
                  "Integral (V·ns)", "Carga (nC)", "Señal/ruido", "Parecido con la forma media"]

# Columnas de la tabla: (identificador, título, ancho en píxeles)
COLUMNAS_DE_LA_TABLA = [
    ("disparo", "Disparo", 70),
    ("canal", "Canal", 55),
    ("kv", "kV", 50),
    ("cm", "cm", 50),
    ("gas", "Gas", 50),
    ("amplitud", "Amplitud (V)", 95),
    ("fwhm", "FWHM (ns)", 80),
    ("subida", "Subida (ns)", 80),
    ("bajada", "Bajada (ns)", 80),
    ("integral", "Integral (V·ns)", 105),
    ("carga", "Carga (nC)", 80),
    ("senal_ruido", "S/R", 55),
    ("parecido", "Parecido", 70),
    ("avisos", "Avisos", 220),
]


# ─── Clases ────────────────────────────────────────────────────────────────

class ArchivoDeDisparo:
    """
    Un archivo .csv guardado por captura_tds684a.py, ya leído.

    Lo crea leer_archivo_de_disparo. Cada canal del archivo se analiza por
    separado con analizar_pulso.

    Atributos
    ---------
    ruta : str
        Ruta completa del archivo.
    nombre : str
        Nombre del archivo sin la carpeta.
    metadatos : dict de str a str
        Líneas "# clave: valor" del encabezado (disparo, gas, fecha, ...).
    ajustes_de_canales : dict de str a dict
        Para cada canal ("CH4"), sus ajustes del encabezado: escala_V_por_div,
        posicion_div, offset_V, impedancia, etc., como texto.
    tiempos_s : numpy.ndarray de float
        Tiempo de cada punto desde el trigger, en s.
    voltajes_por_canal : dict de str a numpy.ndarray de float
        Voltaje de cada punto para cada canal, en V.
    """

    def __init__(self, ruta):
        """
        Crea el archivo vacío; lo llena leer_archivo_de_disparo.

        Parámetros
        ----------
        ruta : str
        """
        self.ruta = ruta
        self.nombre = os.path.basename(ruta)
        self.metadatos = {}
        self.ajustes_de_canales = {}
        self.tiempos_s = None
        self.voltajes_por_canal = {}

    def dato(self, clave):
        """
        Retorna un dato del encabezado como texto, o "" si no está o dice "sin dato".

        Parámetros
        ----------
        clave : str
            Por ejemplo "disparo" o "gas".
        """
        if clave not in self.metadatos:
            return ""
        valor = self.metadatos[clave]
        if valor == "sin dato":
            return ""
        return valor

    def dato_numerico(self, clave):
        """
        Retorna un dato del encabezado como número, o None si falta o no es número.

        Parámetros
        ----------
        clave : str
            Por ejemplo "diferencia_de_potencial_kV".
        """
        texto = self.dato(clave)
        try:
            return float(texto.replace(",", "."))
        except ValueError:
            return None

    def ajuste_numerico(self, canal, clave):
        """
        Retorna un ajuste numérico del canal (por ejemplo su escala), o None.

        Parámetros
        ----------
        canal : str
            Por ejemplo "CH4".
        clave : str
            Por ejemplo "escala_V_por_div".
        """
        if canal not in self.ajustes_de_canales:
            return None
        ajustes = self.ajustes_de_canales[canal]
        if clave not in ajustes:
            return None
        try:
            return float(ajustes[clave])
        except ValueError:
            return None


class ResultadoDelPulso:
    """
    Las mediciones del pulso de un canal de un archivo.

    Lo crea analizar_pulso. La ventana lo muestra en la tabla y los gráficos.
    Los tiempos son desde el trigger, en s. Los que no se pudieron medir
    (por ejemplo si el pulso quedó cortado en el borde del registro) son None.

    Atributos
    ---------
    archivo : ArchivoDeDisparo
    canal : str
    polaridad : int
        +1 si el pulso es positivo, −1 si es negativo.
    linea_base_V, ruido_V : float
        Nivel sin pulso y su ruido (desviación estándar), en V.
    senal_V : numpy.ndarray de float
        (V − línea base) × polaridad: el pulso siempre hacia arriba, en V.
    indice_pico : int
        Posición del pico en el registro.
    tiempo_pico_s, amplitud_V : float
        Tiempo y altura del pico sobre la línea base (positiva), en s y V.
    tiempo_10_entrada_s, tiempo_50_entrada_s, tiempo_90_entrada_s : float o None
        Cruces del 10, 50 y 90 % en el flanco de entrada, en s.
    tiempo_90_salida_s, tiempo_50_salida_s, tiempo_10_salida_s : float o None
        Cruces del 90, 50 y 10 % en el flanco de salida, en s.
    ancho_fwhm_s, subida_s, bajada_s : float o None
        Ancho a media altura y tiempos de subida y bajada, en s.
    integral_Vs : float o None
        Área del pulso entre los cruces del 10 %, en V·s.
    carga_C : float o None
        integral / 50 Ω, en C. None si el canal no estaba en 50 Ω.
    senal_ruido : float
        Amplitud dividida por el ruido.
    esta_saturado : bool
        True si el pulso saturó el digitalizador (la amplitud es una cota inferior).
    parecido : float o None
        Correlación de su forma con la forma media de los pulsos comparados (1 = idéntica).
    avisos : lista de str
    """

    def __init__(self, archivo, canal):
        """
        Crea un resultado vacío; lo llena analizar_pulso.

        Parámetros
        ----------
        archivo : ArchivoDeDisparo
        canal : str
        """
        self.archivo = archivo
        self.canal = canal
        self.polaridad = 1
        self.linea_base_V = 0.0
        self.ruido_V = 0.0
        self.senal_V = None
        self.indice_pico = 0
        self.tiempo_pico_s = 0.0
        self.amplitud_V = 0.0
        self.tiempo_10_entrada_s = None
        self.tiempo_50_entrada_s = None
        self.tiempo_90_entrada_s = None
        self.tiempo_90_salida_s = None
        self.tiempo_50_salida_s = None
        self.tiempo_10_salida_s = None
        self.ancho_fwhm_s = None
        self.subida_s = None
        self.bajada_s = None
        self.integral_Vs = None
        self.carga_C = None
        self.senal_ruido = 0.0
        self.esta_saturado = False
        self.parecido = None
        self.avisos = []

    def etiqueta(self):
        """
        Retorna
        -------
        str
            Nombre corto para leyendas, por ejemplo "2330 · CH4".
        """
        disparo = self.archivo.dato("disparo")
        if disparo == "":
            disparo = self.archivo.nombre
        return f"{disparo} · {self.canal}"


# ─── Funciones: lectura de archivos ────────────────────────────────────────

def interpretar_ajustes_de_canal(texto):
    """
    Separa una línea de ajustes de canal en un diccionario.

    Ejemplo: "escala_V_por_div=1.0e+00 posicion_div=2.94 impedancia=FIFTY"
    da {"escala_V_por_div": "1.0e+00", "posicion_div": "2.94", "impedancia": "FIFTY"}.

    Parámetros
    ----------
    texto : str

    Retorna
    -------
    dict de str a str
    """
    ajustes = {}
    for parte in texto.split(" "):
        if "=" not in parte:
            continue
        posicion_del_igual = parte.find("=")
        clave = parte[:posicion_del_igual]
        valor = parte[posicion_del_igual + 1:]
        ajustes[clave] = valor
    return ajustes


def guardar_linea_de_metadatos(archivo, linea):
    """
    Guarda en el archivo una línea "# clave: valor" del encabezado.

    Las líneas de canal ("# CH4: escala_V_por_div=...") van a
    ajustes_de_canales; el resto, a metadatos.

    Parámetros
    ----------
    archivo : ArchivoDeDisparo
        Se modifica en el lugar.
    linea : str
        Línea completa, empezando con "#".
    """
    contenido = linea[1:].strip()
    if ": " not in contenido:
        return
    posicion = contenido.find(": ")
    clave = contenido[:posicion].strip()
    valor = contenido[posicion + 2:].strip()
    # Las claves de canal son "CH1" a "CH4"
    es_linea_de_canal = clave.startswith("CH") and len(clave) == 3
    if es_linea_de_canal:
        archivo.ajustes_de_canales[clave] = interpretar_ajustes_de_canal(valor)
    else:
        archivo.metadatos[clave] = valor


def leer_archivo_de_disparo(ruta):
    """
    Lee un .csv guardado por captura_tds684a.py.

    Parámetros
    ----------
    ruta : str

    Retorna
    -------
    ArchivoDeDisparo

    Errores
    -------
    ValueError si el archivo no tiene la columna tiempo_s o ninguna columna de canal.
    OSError si no se puede abrir.
    """
    archivo = ArchivoDeDisparo(ruta)
    nombres_de_columnas = None
    filas = []

    # Paso 1: separar encabezado, nombres de columnas y datos
    with open(ruta, encoding="utf-8") as entrada:
        for linea in entrada:
            linea = linea.strip()
            if linea == "":
                continue
            if linea.startswith("#"):
                guardar_linea_de_metadatos(archivo, linea)
            elif nombres_de_columnas is None:
                nombres_de_columnas = linea.split(",")
            else:
                valores = []
                for texto in linea.split(","):
                    valores.append(float(texto))
                filas.append(valores)

    # Paso 2: revisar que tenga lo necesario
    if nombres_de_columnas is None or "tiempo_s" not in nombres_de_columnas:
        raise ValueError(f"{archivo.nombre} no tiene la columna tiempo_s. "
                         "¿Es un archivo guardado por captura_tds684a.py?")

    # Paso 3: pasar las filas a columnas (una tabla de numpy, una columna por nombre)
    tabla = np.array(filas)
    archivo.tiempos_s = tabla[:, nombres_de_columnas.index("tiempo_s")]
    for numero_de_columna in range(len(nombres_de_columnas)):
        nombre = nombres_de_columnas[numero_de_columna]
        # Las columnas de voltaje se llaman "CH4_V"
        if nombre.endswith("_V"):
            canal = nombre[:-2]
            archivo.voltajes_por_canal[canal] = tabla[:, numero_de_columna]
    if len(archivo.voltajes_por_canal) == 0:
        raise ValueError(f"{archivo.nombre} no tiene columnas de voltaje (CH1_V, ...).")
    return archivo


# ─── Funciones: análisis de un pulso ───────────────────────────────────────

def suavizar_senal(senal_V, tiempos_s, suavizado_s):
    """
    Aplica un promedio móvil a la señal para reducir el ruido antes de buscar cruces.

    Cada punto se reemplaza por el promedio de los puntos vecinos dentro de
    una ventana de ancho suavizado_s. El área del pulso no cambia; los flancos
    se ensanchan muy poco si la ventana es mucho menor que el tiempo de subida.

    Parámetros
    ----------
    senal_V : numpy.ndarray de float
    tiempos_s : numpy.ndarray de float
        Para saber cuántos puntos caben en la ventana.
    suavizado_s : float
        Ancho de la ventana, en s. Si es 0 o abarca un solo punto, no se suaviza.

    Retorna
    -------
    numpy.ndarray de float
        Señal suavizada, del mismo largo.
    """
    intervalo_de_muestreo_s = tiempos_s[1] - tiempos_s[0]
    puntos_en_la_ventana = int(round(suavizado_s / intervalo_de_muestreo_s))
    if puntos_en_la_ventana <= 1:
        return senal_V
    # Ventana de pesos iguales que suman 1 (un promedio)
    pesos = np.ones(puntos_en_la_ventana) / puntos_en_la_ventana
    # np.convolve desliza la ventana por la señal; mode="same" mantiene el largo
    return np.convolve(senal_V, pesos, mode="same")


def ajustar_cima(tiempos_s, senal_V, indice_maximo):
    """
    Ajusta una parábola a la cima del pulso para medir su altura y su tiempo.

    Se usan los puntos contiguos al máximo que están sobre FRACCION_CIMA del
    máximo. Si son muy pocos, o la parábola no tiene su vértice dentro de la
    cima, se usa directamente la muestra más alta.

    Parámetros
    ----------
    tiempos_s, senal_V : numpy.ndarray de float
        senal_V es el pulso hacia arriba, sin línea base.
    indice_maximo : int
        Posición de la muestra más alta.

    Retorna
    -------
    tupla (tiempo_pico_s, amplitud_V)
    """
    # Paso 1: extender la cima a ambos lados mientras siga sobre el nivel
    nivel_V = FRACCION_CIMA * senal_V[indice_maximo]
    primero = indice_maximo
    while primero - 1 >= 0 and senal_V[primero - 1] >= nivel_V:
        primero = primero - 1
    ultimo = indice_maximo
    while ultimo + 1 < len(senal_V) and senal_V[ultimo + 1] >= nivel_V:
        ultimo = ultimo + 1
    respuesta_simple = (float(tiempos_s[indice_maximo]), float(senal_V[indice_maximo]))
    if ultimo - primero + 1 < PUNTOS_MINIMOS_PARABOLA:
        return respuesta_simple

    # Paso 2: ajustar V = a·x² + b·x + c, con x medido desde la muestra más alta
    # (restar ese tiempo evita números enormes y errores de redondeo)
    x_s = tiempos_s[primero:ultimo + 1] - tiempos_s[indice_maximo]
    coeficientes = np.polyfit(x_s, senal_V[primero:ultimo + 1], 2)
    a = coeficientes[0]
    b = coeficientes[1]
    c = coeficientes[2]

    # Paso 3: vértice de la parábola, solo si es un máximo dentro de la cima
    if a >= 0:
        return respuesta_simple
    x_vertice_s = -b / (2 * a)
    if x_vertice_s < x_s[0] or x_vertice_s > x_s[-1]:
        return respuesta_simple
    altura_vertice_V = c - b * b / (4 * a)
    return float(tiempos_s[indice_maximo] + x_vertice_s), float(altura_vertice_V)


def calcular_linea_base_y_ruido(voltajes_V):
    """
    Estima el nivel sin pulso y su ruido, sin que el pulso los afecte.

    Parámetros
    ----------
    voltajes_V : numpy.ndarray de float
        Registro completo, en V.

    Retorna
    -------
    tupla (linea_base_V, ruido_V)
        Mediana del registro y desviación estándar estimada con la MAD, en V.
    """
    linea_base_V = float(np.median(voltajes_V))
    # Desviación absoluta de cada punto (opera sobre todo el registro a la vez)
    desviaciones_V = np.abs(voltajes_V - linea_base_V)
    ruido_V = FACTOR_MAD_A_DESVIACION * float(np.median(desviaciones_V))
    return linea_base_V, ruido_V


def calcular_indices_de_busqueda(tiempos_s, inicio_s, fin_s):
    """
    Retorna el primer y el último índice del tramo donde se busca el pulso.

    Parámetros
    ----------
    tiempos_s : numpy.ndarray de float
    inicio_s, fin_s : float o None
        Límites del tramo, en s. None = desde el principio o hasta el final.

    Retorna
    -------
    tupla (indice_inicio, indice_fin)
        Ambos incluidos. Si el tramo no tiene puntos, todo el registro.
    """
    indice_inicio = 0
    indice_fin = len(tiempos_s) - 1
    if inicio_s is not None:
        # searchsorted da la posición del primer tiempo mayor o igual a inicio_s
        indice_inicio = int(np.searchsorted(tiempos_s, inicio_s))
    if fin_s is not None:
        indice_fin = int(np.searchsorted(tiempos_s, fin_s)) - 1
    if indice_inicio > indice_fin or indice_inicio >= len(tiempos_s) or indice_fin < 0:
        return 0, len(tiempos_s) - 1
    return indice_inicio, indice_fin


def elegir_polaridad(sin_base_V, polaridad_pedida):
    """
    Decide si el pulso es positivo o negativo.

    Parámetros
    ----------
    sin_base_V : numpy.ndarray de float
        Voltaje menos la línea base, en el tramo de búsqueda.
    polaridad_pedida : str
        "Automática", "Negativa" o "Positiva".

    Retorna
    -------
    int
        +1 o −1.
    """
    if polaridad_pedida == "Negativa":
        return -1
    if polaridad_pedida == "Positiva":
        return 1
    # Automática: el lado que más se aleja de la línea base
    mayor_subida_V = float(np.max(sin_base_V))
    mayor_bajada_V = -float(np.min(sin_base_V))
    if mayor_bajada_V > mayor_subida_V:
        return -1
    return 1


def buscar_cruce(tiempos_s, senal_V, indice_pico, nivel_V, paso):
    """
    Busca dónde la señal cruza un nivel, caminando desde el pico hacia un lado.

    El tiempo exacto se obtiene interpolando en línea recta entre las dos
    muestras que quedan a cada lado del nivel.

    Parámetros
    ----------
    tiempos_s : numpy.ndarray de float
    senal_V : numpy.ndarray de float
        Pulso hacia arriba (sin línea base y con la polaridad corregida), en V.
    indice_pico : int
    nivel_V : float
        Nivel a cruzar, en V (por ejemplo la mitad de la amplitud).
    paso : int
        −1 para caminar hacia la izquierda (flanco de entrada),
        +1 hacia la derecha (flanco de salida).

    Retorna
    -------
    float o None
        Tiempo del cruce, en s. None si se llegó al borde del registro sin
        cruzar, o si la señal ya está bajo el nivel en el punto de partida.
    """
    if senal_V[indice_pico] < nivel_V:
        return None
    indice = indice_pico
    while 0 <= indice + paso < len(senal_V):
        siguiente = indice + paso
        if senal_V[siguiente] < nivel_V:
            # Fracción del intervalo entre las dos muestras donde ocurre el cruce
            fraccion = (senal_V[indice] - nivel_V) / (senal_V[indice] - senal_V[siguiente])
            return float(tiempos_s[indice] + fraccion * (tiempos_s[siguiente] - tiempos_s[indice]))
        indice = siguiente
    return None


def restar_si_existen(tiempo_final_s, tiempo_inicial_s):
    """
    Retorna tiempo_final − tiempo_inicial, o None si alguno de los dos es None.

    Parámetros
    ----------
    tiempo_final_s, tiempo_inicial_s : float o None
    """
    if tiempo_final_s is None or tiempo_inicial_s is None:
        return None
    return tiempo_final_s - tiempo_inicial_s


def medir_cruces_de_un_flanco(tiempos_s, senal_V, indice_pico, amplitud_V, paso, suavizado_s):
    """
    Busca los cruces del 10, 50 y 90 % en un flanco, sobre la señal suavizada.

    Si en la señal suavizada algún cruce no se encuentra (por ejemplo porque
    el suavizado bajó la cima bajo el 90 %), se usa el de la señal sin suavizar.

    Parámetros
    ----------
    tiempos_s, senal_V : numpy.ndarray de float
        senal_V es el pulso hacia arriba, sin línea base.
    indice_pico : int
    amplitud_V : float
    paso : int
        −1 para el flanco de entrada, +1 para el de salida.
    suavizado_s : float
        Ancho del promedio móvil, en s (0 = sin suavizar).

    Retorna
    -------
    dict de float a (float o None)
        Tiempo de cruce, en s, para cada fracción (FRACCION_BAJA, FRACCION_MITAD, FRACCION_ALTA).
    """
    senal_suavizada_V = suavizar_senal(senal_V, tiempos_s, suavizado_s)
    cruces = {}
    for fraccion in [FRACCION_BAJA, FRACCION_MITAD, FRACCION_ALTA]:
        nivel_V = fraccion * amplitud_V
        tiempo_s = buscar_cruce(tiempos_s, senal_suavizada_V, indice_pico, nivel_V, paso)
        if tiempo_s is None:
            tiempo_s = buscar_cruce(tiempos_s, senal_V, indice_pico, nivel_V, paso)
        cruces[fraccion] = tiempo_s
    return cruces


def medir_tiempos_del_pulso(resultado, tiempos_s):
    """
    Mide los cruces del 10, 50 y 90 % y con ellos el FWHM, la subida y la bajada.

    Se hace en dos pasadas. La primera, sin suavizar, da una idea de cuánto
    dura cada flanco. La segunda suaviza cada flanco con una ventana igual a
    FRACCION_DEL_FLANCO_PARA_SUAVIZAR de su propia duración: el flanco rápido
    casi no se toca y la cola lenta se suaviza lo necesario para que el ruido
    no adelante el cruce.

    Parámetros
    ----------
    resultado : ResultadoDelPulso
        Debe traer senal_V, indice_pico y amplitud_V. Se modifica en el lugar.
    tiempos_s : numpy.ndarray de float
    """
    senal_V = resultado.senal_V
    pico = resultado.indice_pico
    amplitud_V = resultado.amplitud_V

    # Paso 1: primera pasada, sin suavizar
    entrada = medir_cruces_de_un_flanco(tiempos_s, senal_V, pico, amplitud_V, -1, 0.0)
    salida = medir_cruces_de_un_flanco(tiempos_s, senal_V, pico, amplitud_V, 1, 0.0)

    # Paso 2: segunda pasada, cada flanco suavizado según su duración
    duracion_entrada_s = restar_si_existen(entrada[FRACCION_ALTA], entrada[FRACCION_BAJA])
    duracion_salida_s = restar_si_existen(salida[FRACCION_BAJA], salida[FRACCION_ALTA])
    if duracion_entrada_s is not None:
        entrada = medir_cruces_de_un_flanco(tiempos_s, senal_V, pico, amplitud_V, -1,
                                            FRACCION_DEL_FLANCO_PARA_SUAVIZAR * duracion_entrada_s)
    if duracion_salida_s is not None:
        salida = medir_cruces_de_un_flanco(tiempos_s, senal_V, pico, amplitud_V, 1,
                                           FRACCION_DEL_FLANCO_PARA_SUAVIZAR * duracion_salida_s)

    # Paso 3: guardar los cruces
    resultado.tiempo_10_entrada_s = entrada[FRACCION_BAJA]
    resultado.tiempo_50_entrada_s = entrada[FRACCION_MITAD]
    resultado.tiempo_90_entrada_s = entrada[FRACCION_ALTA]
    resultado.tiempo_90_salida_s = salida[FRACCION_ALTA]
    resultado.tiempo_50_salida_s = salida[FRACCION_MITAD]
    resultado.tiempo_10_salida_s = salida[FRACCION_BAJA]

    # Paso 4: anchos
    resultado.ancho_fwhm_s = restar_si_existen(resultado.tiempo_50_salida_s, resultado.tiempo_50_entrada_s)
    resultado.subida_s = restar_si_existen(resultado.tiempo_90_entrada_s, resultado.tiempo_10_entrada_s)
    resultado.bajada_s = restar_si_existen(resultado.tiempo_10_salida_s, resultado.tiempo_90_salida_s)
    if resultado.ancho_fwhm_s is None:
        resultado.avisos.append("pulso cortado en el borde del registro")


def integrar_por_trapecios(tiempos_s, senal_V, tiempo_inicio_s, tiempo_fin_s):
    """
    Integra la señal entre dos tiempos sumando trapecios entre muestras.

    Parámetros
    ----------
    tiempos_s, senal_V : numpy.ndarray de float
    tiempo_inicio_s, tiempo_fin_s : float
        Límites de la integral, en s.

    Retorna
    -------
    float
        Integral en V·s.
    """
    indice_inicio = int(np.searchsorted(tiempos_s, tiempo_inicio_s))
    indice_fin = int(np.searchsorted(tiempos_s, tiempo_fin_s)) - 1
    integral_Vs = 0.0
    for i in range(indice_inicio, indice_fin):
        # Área del trapecio entre la muestra i y la i+1
        base_s = tiempos_s[i + 1] - tiempos_s[i]
        altura_media_V = (senal_V[i] + senal_V[i + 1]) / 2.0
        integral_Vs = integral_Vs + base_s * altura_media_V
    return float(integral_Vs)


def revisar_saturacion(resultado):
    """
    Revisa si el pulso llegó al límite del digitalizador.

    Usa la escala, la posición y el offset del canal guardados en el
    encabezado para pasar cada voltaje a divisiones de la pantalla:
    divisiones = (V − offset) / escala + posición.

    Parámetros
    ----------
    resultado : ResultadoDelPulso
        Debe traer los cruces del 10 %. Se modifica en el lugar.
    """
    archivo = resultado.archivo
    escala_V = archivo.ajuste_numerico(resultado.canal, "escala_V_por_div")
    posicion_div = archivo.ajuste_numerico(resultado.canal, "posicion_div")
    offset_V = archivo.ajuste_numerico(resultado.canal, "offset_V")
    if escala_V is None or posicion_div is None or offset_V is None or escala_V == 0:
        return

    # Tramo del pulso: entre los cruces del 10 %, o solo el pico si faltan
    inicio_s = resultado.tiempo_10_entrada_s
    fin_s = resultado.tiempo_10_salida_s
    if inicio_s is None or fin_s is None:
        inicio_s = resultado.tiempo_pico_s
        fin_s = resultado.tiempo_pico_s
    indice_inicio, indice_fin = calcular_indices_de_busqueda(archivo.tiempos_s, inicio_s, fin_s)
    indice_fin = max(indice_fin, resultado.indice_pico)
    indice_inicio = min(indice_inicio, resultado.indice_pico)

    # Altura en pantalla del tramo (opera sobre el tramo completo a la vez)
    voltajes_del_tramo_V = archivo.voltajes_por_canal[resultado.canal][indice_inicio:indice_fin + 1]
    divisiones = (voltajes_del_tramo_V - offset_V) / escala_V + posicion_div
    if float(np.max(np.abs(divisiones))) >= DIVISIONES_DE_SATURACION:
        resultado.esta_saturado = True
        resultado.avisos.append("saturado: la amplitud real es mayor")


def analizar_pulso(archivo, canal, polaridad_pedida, inicio_busqueda_s, fin_busqueda_s):
    """
    Mide el pulso de un canal: línea base, ruido, amplitud, anchos, integral y carga.

    Parámetros
    ----------
    archivo : ArchivoDeDisparo
    canal : str
    polaridad_pedida : str
        "Automática", "Negativa" o "Positiva".
    inicio_busqueda_s, fin_busqueda_s : float o None
        Tramo donde buscar el pico, en s desde el trigger. None = todo el registro.

    Retorna
    -------
    ResultadoDelPulso
    """
    tiempos_s = archivo.tiempos_s
    voltajes_V = archivo.voltajes_por_canal[canal]
    resultado = ResultadoDelPulso(archivo, canal)

    # Paso 1: línea base y ruido
    resultado.linea_base_V, resultado.ruido_V = calcular_linea_base_y_ruido(voltajes_V)

    # Paso 2: polaridad y señal "hacia arriba" (operaciones sobre todo el registro)
    indice_inicio, indice_fin = calcular_indices_de_busqueda(tiempos_s, inicio_busqueda_s, fin_busqueda_s)
    sin_base_V = voltajes_V - resultado.linea_base_V
    resultado.polaridad = elegir_polaridad(sin_base_V[indice_inicio:indice_fin + 1], polaridad_pedida)
    resultado.senal_V = sin_base_V * resultado.polaridad

    # Paso 3: pico dentro del tramo de búsqueda, con la parábola ajustada a la cima
    posicion_en_el_tramo = int(np.argmax(resultado.senal_V[indice_inicio:indice_fin + 1]))
    resultado.indice_pico = indice_inicio + posicion_en_el_tramo
    resultado.tiempo_pico_s, resultado.amplitud_V = ajustar_cima(tiempos_s, resultado.senal_V,
                                                                 resultado.indice_pico)

    # Paso 4: tiempos de cruce y anchos
    medir_tiempos_del_pulso(resultado, tiempos_s)

    # Paso 5: integral entre los cruces del 10 % y carga si el canal estaba en 50 Ω
    if resultado.tiempo_10_entrada_s is not None and resultado.tiempo_10_salida_s is not None:
        resultado.integral_Vs = integrar_por_trapecios(tiempos_s, resultado.senal_V,
                                                       resultado.tiempo_10_entrada_s, resultado.tiempo_10_salida_s)
        impedancia = ""
        if canal in archivo.ajustes_de_canales and "impedancia" in archivo.ajustes_de_canales[canal]:
            impedancia = archivo.ajustes_de_canales[canal]["impedancia"]
        if impedancia.upper() == "FIFTY":
            resultado.carga_C = resultado.integral_Vs / RESISTENCIA_DE_ENTRADA_OHM

    # Paso 6: razón señal/ruido y avisos
    if resultado.ruido_V > 0:
        resultado.senal_ruido = resultado.amplitud_V / resultado.ruido_V
    if resultado.senal_ruido < SENAL_RUIDO_MINIMA:
        resultado.avisos.append(f"S/R menor que {SENAL_RUIDO_MINIMA:g}: pulso poco claro")
    revisar_saturacion(resultado)
    return resultado


# ─── Funciones: comparación de formas ──────────────────────────────────────

def tiempo_de_alineacion(resultado, alineacion):
    """
    Retorna el tiempo que se usa como cero al superponer pulsos.

    Parámetros
    ----------
    resultado : ResultadoDelPulso
    alineacion : str
        "Flanco al 50 %" o "Pico". El flanco es más estable que el pico
        cuando el pico es ancho o ruidoso.

    Retorna
    -------
    float
        En s. Si no hay cruce del 50 %, se usa el pico.
    """
    if alineacion == "Flanco al 50 %" and resultado.tiempo_50_entrada_s is not None:
        return resultado.tiempo_50_entrada_s
    return resultado.tiempo_pico_s


def calcular_tramo_de_comparacion(resultados):
    """
    Calcula cuánto tiempo mostrar antes y después del punto de alineación.

    Parámetros
    ----------
    resultados : lista de ResultadoDelPulso

    Retorna
    -------
    tupla (antes_s, despues_s)
        Ambos positivos, en s, proporcionales al FWHM más ancho.
    """
    ancho_mayor_s = 0.0
    for resultado in resultados:
        if resultado.ancho_fwhm_s is not None and resultado.ancho_fwhm_s > ancho_mayor_s:
            ancho_mayor_s = resultado.ancho_fwhm_s
    if ancho_mayor_s == 0.0:
        # Sin anchos medidos: 50 ns antes y 150 ns después
        ancho_mayor_s = 25 * SEGUNDOS_POR_NANOSEGUNDO
    return ANCHOS_ANTES_EN_COMPARACION * ancho_mayor_s, ANCHOS_DESPUES_EN_COMPARACION * ancho_mayor_s


def calcular_forma_en_grilla(resultado, alineacion, normalizar, grilla_s):
    """
    Lleva un pulso a una grilla común de tiempos, alineado (y normalizado si se pide).

    Parámetros
    ----------
    resultado : ResultadoDelPulso
    alineacion : str
        "Flanco al 50 %" o "Pico".
    normalizar : bool
        True para dividir por la amplitud (pico = 1).
    grilla_s : numpy.ndarray de float
        Tiempos relativos al punto de alineación, en s.

    Retorna
    -------
    numpy.ndarray de float
        Valor del pulso en cada tiempo de la grilla (NaN fuera del registro).
    """
    tiempos_relativos_s = resultado.archivo.tiempos_s - tiempo_de_alineacion(resultado, alineacion)
    valores = resultado.senal_V
    if normalizar and resultado.amplitud_V != 0:
        valores = resultado.senal_V / resultado.amplitud_V
    # np.interp calcula el valor en cada tiempo de la grilla uniendo muestras con rectas
    return np.interp(grilla_s, tiempos_relativos_s, valores, left=np.nan, right=np.nan)


def calcular_parecido(forma, forma_media):
    """
    Mide qué tan parecida es una forma a la forma media (coeficiente de correlación).

    Vale 1 si las formas son iguales salvo un factor de escala, y baja hacia
    0 a medida que se diferencian.

    Parámetros
    ----------
    forma, forma_media : numpy.ndarray de float
        En la misma grilla. Los NaN se ignoran.

    Retorna
    -------
    float o None
        None si quedan menos de 3 puntos en común.
    """
    # Solo los puntos donde ambas formas tienen valor
    hay_valor = np.isfinite(forma) & np.isfinite(forma_media)
    if np.sum(hay_valor) < 3:
        return None
    x = forma[hay_valor]
    y = forma_media[hay_valor]
    # Coeficiente de correlación de Pearson: covarianza / (desviación x · desviación y)
    x_centrado = x - np.mean(x)
    y_centrado = y - np.mean(y)
    denominador = np.sqrt(np.sum(x_centrado ** 2) * np.sum(y_centrado ** 2))
    if denominador == 0:
        return None
    return float(np.sum(x_centrado * y_centrado) / denominador)


def comparar_formas(resultados, alineacion, normalizar):
    """
    Lleva los pulsos a una grilla común, calcula la forma media y el parecido de cada uno.

    Parámetros
    ----------
    resultados : lista de ResultadoDelPulso
        Se les escribe el atributo parecido.
    alineacion : str
    normalizar : bool

    Retorna
    -------
    tupla (grilla_s, formas, forma_media, desviacion)
        grilla_s : numpy.ndarray, tiempos relativos al punto de alineación, en s.
        formas : lista de numpy.ndarray, una por resultado.
        forma_media, desviacion : numpy.ndarray, promedio y desviación estándar
        punto a punto (None si hay menos de 2 pulsos).
    """
    # Paso 1: grilla común
    antes_s, despues_s = calcular_tramo_de_comparacion(resultados)
    grilla_s = np.linspace(-antes_s, despues_s, PUNTOS_DE_LA_FORMA_MEDIA)

    # Paso 2: cada pulso en la grilla
    formas = []
    for resultado in resultados:
        formas.append(calcular_forma_en_grilla(resultado, alineacion, normalizar, grilla_s))

    # Paso 3: forma media y parecido (necesita al menos dos pulsos)
    forma_media = None
    desviacion = None
    for resultado in resultados:
        resultado.parecido = None
    if len(formas) >= 2:
        # np.array apila las formas en una tabla: una fila por pulso
        tabla_de_formas = np.array(formas)
        # nanmean y nanstd promedian cada columna ignorando los NaN
        forma_media = np.nanmean(tabla_de_formas, axis=0)
        desviacion = np.nanstd(tabla_de_formas, axis=0)
        for i in range(len(resultados)):
            resultados[i].parecido = calcular_parecido(formas[i], forma_media)
    return grilla_s, formas, forma_media, desviacion


# ─── Funciones: tabla y exportación ────────────────────────────────────────

def formatear(valor, factor, decimales):
    """
    Pasa un número a texto en la unidad de la tabla.

    Parámetros
    ----------
    valor : float o None
    factor : float
        Se divide el valor por este factor (por ejemplo 1e-9 para mostrar ns).
    decimales : int

    Retorna
    -------
    str
        "—" si el valor es None.
    """
    if valor is None:
        return "—"
    return f"{valor / factor:.{decimales}f}"


def valores_de_la_fila(resultado):
    """
    Arma los textos de una fila de la tabla, en el orden de COLUMNAS_DE_LA_TABLA.

    Parámetros
    ----------
    resultado : ResultadoDelPulso

    Retorna
    -------
    lista de str
    """
    archivo = resultado.archivo
    integral_V_ns = None
    if resultado.integral_Vs is not None:
        integral_V_ns = resultado.integral_Vs / SEGUNDOS_POR_NANOSEGUNDO
    amplitud_texto = formatear(resultado.amplitud_V, 1.0, 3)
    # Un pulso saturado se marca con ≥: la amplitud real es mayor
    if resultado.esta_saturado:
        amplitud_texto = "≥ " + amplitud_texto
    fila = [archivo.dato("disparo"), resultado.canal, archivo.dato("diferencia_de_potencial_kV"),
            archivo.dato("distancia_detector_cm"), archivo.dato("gas"), amplitud_texto,
            formatear(resultado.ancho_fwhm_s, SEGUNDOS_POR_NANOSEGUNDO, 2),
            formatear(resultado.subida_s, SEGUNDOS_POR_NANOSEGUNDO, 2),
            formatear(resultado.bajada_s, SEGUNDOS_POR_NANOSEGUNDO, 2),
            formatear(integral_V_ns, 1.0, 2),
            formatear(resultado.carga_C, COULOMB_POR_NANOCOULOMB, 3),
            formatear(resultado.senal_ruido, 1.0, 0),
            formatear(resultado.parecido, 1.0, 3),
            "; ".join(resultado.avisos)]
    return fila


def exportar_tabla(ruta, resultados):
    """
    Escribe un .csv con las mediciones de todos los pulsos, en unidades base.

    Parámetros
    ----------
    ruta : str
    resultados : lista de ResultadoDelPulso

    Errores
    -------
    OSError si no se puede escribir.
    """
    columnas = ["archivo", "disparo", "canal", "diferencia_de_potencial_kV", "distancia_detector_cm", "gas",
                "polaridad", "linea_base_V", "ruido_V", "tiempo_pico_s", "amplitud_V", "saturado",
                "fwhm_s", "subida_s", "bajada_s", "integral_Vs", "carga_C", "senal_ruido", "parecido", "avisos"]
    with open(ruta, "w", encoding="utf-8", newline="") as salida:
        salida.write(",".join(columnas) + "\n")
        for resultado in resultados:
            archivo = resultado.archivo
            valores = [archivo.nombre, archivo.dato("disparo"), resultado.canal,
                       archivo.dato("diferencia_de_potencial_kV"), archivo.dato("distancia_detector_cm"),
                       archivo.dato("gas"), str(resultado.polaridad), f"{resultado.linea_base_V:.6e}", f"{resultado.ruido_V:.6e}",
                       f"{resultado.tiempo_pico_s:.6e}", f"{resultado.amplitud_V:.6e}", str(resultado.esta_saturado),
                       texto_cientifico(resultado.ancho_fwhm_s), texto_cientifico(resultado.subida_s),
                       texto_cientifico(resultado.bajada_s), texto_cientifico(resultado.integral_Vs),
                       texto_cientifico(resultado.carga_C), f"{resultado.senal_ruido:.1f}", texto_cientifico(resultado.parecido),
                       # Los avisos van entre comillas porque pueden tener comas
                       '"' + "; ".join(resultado.avisos) + '"']
            salida.write(",".join(valores) + "\n")


def texto_cientifico(valor):
    """
    Parámetros
    ----------
    valor : float o None

    Retorna
    -------
    str
        El número en notación científica, o "" si es None (celda vacía en el .csv).
    """
    if valor is None:
        return ""
    return f"{valor:.6e}"


def valor_para_eje(resultado, opcion):
    """
    Retorna el valor de un resultado para un eje del gráfico de tendencias.

    Parámetros
    ----------
    resultado : ResultadoDelPulso
    opcion : str
        Una de OPCIONES_EJE_X u OPCIONES_EJE_Y.

    Retorna
    -------
    float o None
        En las unidades del nombre de la opción. None si no hay dato.
    """
    archivo = resultado.archivo
    if opcion == "Diferencia de potencial (kV)":
        return archivo.dato_numerico("diferencia_de_potencial_kV")
    if opcion == "Distancia del detector (cm)":
        return archivo.dato_numerico("distancia_detector_cm")
    if opcion == "Número de disparo":
        return archivo.dato_numerico("disparo")
    if opcion == "Amplitud (V)":
        return resultado.amplitud_V
    if opcion == "Señal/ruido":
        return resultado.senal_ruido
    if opcion == "Parecido con la forma media":
        return resultado.parecido
    # El resto son tiempos o integrales que se muestran en ns o nC
    valores_en_unidades_base = {
        "Ancho FWHM (ns)": resultado.ancho_fwhm_s,
        "Subida 10–90 % (ns)": resultado.subida_s,
        "Bajada 90–10 % (ns)": resultado.bajada_s,
        "Integral (V·ns)": resultado.integral_Vs,
        "Carga (nC)": resultado.carga_C,
    }
    valor = valores_en_unidades_base[opcion]
    if valor is None:
        return None
    return valor / SEGUNDOS_POR_NANOSEGUNDO


def convertir_texto_a_tiempo_s(texto, nombre_del_campo):
    """
    Convierte un tiempo escrito en ns a segundos.

    Parámetros
    ----------
    texto : str
        Por ejemplo "1700" o "1700,5". Vacío = sin límite.
    nombre_del_campo : str
        Para el mensaje de error.

    Retorna
    -------
    float o None
        En s, o None si el campo está vacío.

    Errores
    -------
    ValueError con un mensaje para el usuario.
    """
    limpio = texto.strip().replace(",", ".")
    if limpio == "":
        return None
    try:
        return float(limpio) * SEGUNDOS_POR_NANOSEGUNDO
    except ValueError:
        raise ValueError(f"«{nombre_del_campo}» debe ser un tiempo en ns, por ejemplo 1700.")


# ─── Estilo de la interfaz (de la plantilla de interfaces) ─────────────────

def _pick_font(root, preferred, fallback="TkDefaultFont"):
    """
    Devuelve la primera tipografía de la lista que esté instalada.

    Parámetros
    ----------
    root : tk.Tk
    preferred : lista de str
    fallback : str

    Retorna
    -------
    str
    """
    available = set(font.families(root))
    for fam in preferred:
        if fam in available:
            return fam
    return fallback


def setup_style(root):
    """
    Configura el tema visual de ttk con los tokens de diseño.

    Parámetros
    ----------
    root : tk.Tk

    Retorna
    -------
    dict
        Tipografías por uso.
    """
    style = ttk.Style(root)
    style.theme_use("clam")

    # Paso 1: tipografías
    ui = _pick_font(root, ["Segoe UI", "SF Pro Text", "Helvetica Neue", "DejaVu Sans"])
    mono = _pick_font(root, ["Cascadia Code", "Consolas", "Menlo", "DejaVu Sans Mono"])
    FONTS = {
        "title":   (ui, 17, "bold"),
        "heading": (ui, 12, "bold"),
        "body":    (ui, 10),
        "muted":   (ui, 9),
        "readout": (mono, 24, "bold"),
        "mono":    (mono, 9),
    }

    # Paso 2: estilo general y contenedores
    root.configure(bg=COLORS["bg"])
    style.configure(".", background=COLORS["bg"], foreground=COLORS["text"],
                    font=FONTS["body"], focuscolor=COLORS["accent"])
    style.configure("App.TFrame", background=COLORS["bg"])
    style.configure("Card.TFrame", background=COLORS["surface"],
                    bordercolor=COLORS["border"], borderwidth=1, relief="solid")

    # Paso 3: etiquetas
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Title.TLabel", font=FONTS["title"])
    style.configure("Heading.TLabel", font=FONTS["heading"])
    style.configure("Muted.TLabel", foreground=COLORS["muted"], font=FONTS["muted"])
    style.configure("Card.TLabel", background=COLORS["surface"])
    style.configure("CardMuted.TLabel", background=COLORS["surface"],
                    foreground=COLORS["muted"], font=FONTS["muted"])
    style.configure("CardHeading.TLabel", background=COLORS["surface"], font=FONTS["heading"])
    style.configure("Card.TCheckbutton", background=COLORS["surface"])

    # Paso 4: botones
    style.configure("TButton", font=FONTS["body"], padding=(PAD_L, PAD_M),
                    background=COLORS["surface"], foreground=COLORS["text"],
                    bordercolor=COLORS["border"], relief="solid", borderwidth=1)
    style.map("TButton", background=[("active", "#EEF1F5"), ("pressed", "#E4E9EF")])
    style.configure("Accent.TButton", background=COLORS["accent"],
                    foreground=COLORS["accent_fg"], bordercolor=COLORS["accent"])
    style.map("Accent.TButton",
              background=[("active", "#26538F"), ("pressed", "#1F4576"), ("disabled", "#AEBACB")])

    # Paso 5: campos, pestañas y tabla
    style.configure("TEntry", fieldbackground=COLORS["surface"], bordercolor=COLORS["border"], padding=PAD_S)
    style.configure("TCombobox", fieldbackground=COLORS["surface"], bordercolor=COLORS["border"], padding=PAD_S)
    style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(PAD_L, PAD_S), background=COLORS["bg"])
    style.map("TNotebook.Tab", background=[("selected", COLORS["surface"])])
    style.configure("Treeview", background=COLORS["surface"], fieldbackground=COLORS["surface"],
                    foreground=COLORS["text"], font=FONTS["muted"], rowheight=22)
    style.configure("Treeview.Heading", background=COLORS["bg"], foreground=COLORS["muted"],
                    font=FONTS["muted"], relief="flat")
    style.map("Treeview", background=[("selected", "#DCE6F5")], foreground=[("selected", COLORS["text"])])
    return FONTS


def estilizar_ejes(ejes):
    """
    Da a un gráfico de matplotlib el estilo de la interfaz.

    Parámetros
    ----------
    ejes : matplotlib.axes.Axes
    """
    ejes.set_facecolor(COLORS["surface"])
    ejes.spines["top"].set_visible(False)
    ejes.spines["right"].set_visible(False)
    ejes.spines["left"].set_color(COLORS["border"])
    ejes.spines["bottom"].set_color(COLORS["border"])
    ejes.tick_params(colors=COLORS["muted"], labelsize=8)
    ejes.grid(True, color=COLORS["border"], linewidth=0.6, alpha=0.7)
    ejes.xaxis.label.set_color(COLORS["muted"])
    ejes.yaxis.label.set_color(COLORS["muted"])


def escribir_mensaje_vacio(ejes, texto):
    """
    Deja el gráfico vacío con un mensaje al centro.

    Parámetros
    ----------
    ejes : matplotlib.axes.Axes
    texto : str
    """
    ejes.text(0.5, 0.5, texto, transform=ejes.transAxes, ha="center", va="center", color=COLORS["muted"])
    ejes.set_xticks([])
    ejes.set_yticks([])


# ─── Ventana principal ─────────────────────────────────────────────────────

class App(ttk.Frame):
    """
    Ventana principal del analizador: lista de archivos, opciones, tabla de
    mediciones y tres pestañas de gráficos.

    La crea main(). Hereda de ttk.Frame (ver encabezado del archivo).

    Atributos principales
    ---------------------
    escala_pantalla : float
        Factor de tamaño según la resolución de la pantalla.
    archivos : lista de ArchivoDeDisparo
        Archivos cargados.
    resultados : lista de ResultadoDelPulso
        Un resultado por canal de cada archivo, ordenados por disparo.
    cola_de_resultados : queue.Queue
        Por aquí el hilo de lectura entrega los archivos a la ventana.
    hay_tarea_en_curso : bool
        True mientras el hilo lee archivos.
    id_revision_pendiente : str o None
        Identificador del próximo after() que revisa la cola.
    """

    def __init__(self, master, escala_pantalla):
        """
        Crea la ventana.

        Parámetros
        ----------
        master : tk.Tk
        escala_pantalla : float
        """
        # Inicializa la parte de ttk.Frame (contenedor con margen y estilo)
        super().__init__(master, padding=PAD_XL, style="App.TFrame")
        self.escala_pantalla = escala_pantalla

        # Paso 1: estado
        self.archivos = []
        self.resultados = []
        self.cola_de_resultados = queue.Queue()
        self.hay_tarea_en_curso = False
        self.id_revision_pendiente = None

        # Paso 2: el contenedor crece con la ventana; crece la columna de gráficos
        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(2, weight=2)

        # Paso 3: construir y dibujar el estado inicial
        self._build()
        self.redibujar_todo()
        self.set_status("Agrega los .csv guardados por captura_tds684a.py.", "idle")

    # ── Construcción de la ventana ────────────────────────────────────────

    def _build(self):
        """Crea el título, el panel de opciones, las pestañas, la tabla y la barra de estado."""
        titulo = ttk.Label(self, text="Análisis de pulsos", style="Title.TLabel")
        titulo.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, PAD_L))
        self._build_panel_de_opciones()
        self._build_pestanas()
        self._build_tabla()
        self._build_statusbar()

    def crear_encabezado(self, fila, texto):
        """
        Pone el título de una sección del panel de opciones.

        Parámetros
        ----------
        fila : int
        texto : str

        Retorna
        -------
        int
            Siguiente fila libre.
        """
        espacio_arriba = PAD_L
        if fila == 0:
            espacio_arriba = 0
        etiqueta = ttk.Label(self.panel, text=texto, style="CardHeading.TLabel")
        etiqueta.grid(row=fila, column=0, columnspan=2, sticky="w", pady=(espacio_arriba, PAD_S))
        return fila + 1

    def crear_lista(self, fila, texto_etiqueta, opciones, accion):
        """
        Pone una etiqueta y una lista desplegable (sin escritura) en el panel.

        Parámetros
        ----------
        fila : int
        texto_etiqueta : str
        opciones : lista de str
        accion : método
            Se ejecuta cuando se elige una opción.

        Retorna
        -------
        tk.StringVar
            Variable con la opción elegida (parte en la primera).
        """
        etiqueta = ttk.Label(self.panel, text=texto_etiqueta, style="Card.TLabel")
        etiqueta.grid(row=fila, column=0, sticky="w", padx=(0, PAD_M), pady=PAD_S)
        variable = tk.StringVar(value=opciones[0])
        lista = ttk.Combobox(self.panel, textvariable=variable, values=opciones, state="readonly", width=24)
        lista.grid(row=fila, column=1, sticky="ew", pady=PAD_S)
        # Avisar a "accion" cada vez que se elige otra opción
        lista.bind("<<ComboboxSelected>>", accion)
        return variable

    def _build_panel_de_opciones(self):
        """Crea la tarjeta de la izquierda: archivos, análisis, comparación y tendencias."""
        self.panel = ttk.Frame(self, style="Card.TFrame", padding=PAD_L)
        self.panel.grid(row=1, column=0, rowspan=2, sticky="ns", padx=(0, PAD_L))

        # Sección 1: archivos
        fila = self.crear_encabezado(0, "1 · Archivos")
        self.boton_agregar = ttk.Button(self.panel, text="Agregar archivos…", style="Accent.TButton",
                                        command=self.al_agregar_archivos)
        self.boton_agregar.grid(row=fila, column=0, sticky="ew", padx=(0, PAD_M), pady=PAD_S)
        boton_quitar = ttk.Button(self.panel, text="Quitar seleccionados", command=self.al_quitar_seleccionados)
        boton_quitar.grid(row=fila, column=1, sticky="ew", pady=PAD_S)
        fila = fila + 1
        self.etiqueta_archivos = ttk.Label(self.panel, text="Sin archivos", style="CardMuted.TLabel")
        self.etiqueta_archivos.grid(row=fila, column=0, columnspan=2, sticky="w")
        fila = fila + 1

        # Sección 2: análisis de cada pulso
        fila = self.crear_encabezado(fila, "2 · Análisis")
        self.polaridad = self.crear_lista(fila, "Polaridad", OPCIONES_DE_POLARIDAD, self.al_cambiar_analisis)
        fila = fila + 1
        fila = self._build_tramo_de_busqueda(fila)

        # Sección 3: comparación de formas
        fila = self.crear_encabezado(fila, "3 · Comparar formas")
        self.alineacion = self.crear_lista(fila, "Alinear por", OPCIONES_DE_ALINEACION, self.al_cambiar_vista)
        fila = fila + 1
        self.normalizar = tk.BooleanVar(value=True)
        casilla = ttk.Checkbutton(self.panel, text="Normalizar a amplitud 1", variable=self.normalizar,
                                  command=self.al_cambiar_vista, style="Card.TCheckbutton")
        casilla.grid(row=fila, column=0, columnspan=2, sticky="w", pady=PAD_S)
        fila = fila + 1
        nota = ttk.Label(self.panel, style="CardMuted.TLabel", justify="left",
                         wraplength=int(300 * self.escala_pantalla),
                         text="Se comparan las filas seleccionadas en la tabla (Ctrl o Mayús para "
                              "elegir varias); si hay menos de dos, todas.")
        nota.grid(row=fila, column=0, columnspan=2, sticky="w")
        fila = fila + 1

        # Sección 4: tendencias
        fila = self.crear_encabezado(fila, "4 · Tendencias")
        self.eje_x_tendencias = self.crear_lista(fila, "Eje horizontal", OPCIONES_EJE_X, self.al_cambiar_vista)
        fila = fila + 1
        self.eje_y_tendencias = self.crear_lista(fila, "Eje vertical", OPCIONES_EJE_Y, self.al_cambiar_vista)
        fila = fila + 1

        # Exportar
        boton_exportar = ttk.Button(self.panel, text="Exportar tabla…", command=self.al_exportar)
        boton_exportar.grid(row=fila, column=0, columnspan=2, sticky="ew", pady=(PAD_L, 0))

    def _build_tramo_de_busqueda(self, fila):
        """
        Crea los campos del tramo donde se busca el pulso y su botón.

        Parámetros
        ----------
        fila : int

        Retorna
        -------
        int
            Siguiente fila libre.
        """
        etiqueta = ttk.Label(self.panel, text="Buscar entre (ns)", style="Card.TLabel")
        etiqueta.grid(row=fila, column=0, sticky="w", padx=(0, PAD_M), pady=PAD_S)
        contenedor = ttk.Frame(self.panel, style="Card.TFrame", borderwidth=0)
        contenedor.grid(row=fila, column=1, sticky="w", pady=PAD_S)
        self.texto_inicio_busqueda = tk.StringVar(value="")
        self.texto_fin_busqueda = tk.StringVar(value="")
        campo_inicio = ttk.Entry(contenedor, textvariable=self.texto_inicio_busqueda, width=8)
        campo_inicio.grid(row=0, column=0)
        separador = ttk.Label(contenedor, text=" y ", style="Card.TLabel")
        separador.grid(row=0, column=1)
        campo_fin = ttk.Entry(contenedor, textvariable=self.texto_fin_busqueda, width=8)
        campo_fin.grid(row=0, column=2)
        fila = fila + 1
        nota = ttk.Label(self.panel, style="CardMuted.TLabel", justify="left",
                         wraplength=int(300 * self.escala_pantalla),
                         text="Vacío = todo el registro. Úsalo si hay ruido de la descarga "
                              "más grande que el pulso.")
        nota.grid(row=fila, column=0, columnspan=2, sticky="w")
        fila = fila + 1
        boton_aplicar = ttk.Button(self.panel, text="Volver a analizar", command=self.al_cambiar_analisis)
        boton_aplicar.grid(row=fila, column=0, columnspan=2, sticky="ew", pady=PAD_S)
        return fila + 1

    def crear_pestana_con_grafico(self, cuaderno, titulo):
        """
        Crea una pestaña con un gráfico de matplotlib y su barra de zoom.

        Parámetros
        ----------
        cuaderno : ttk.Notebook
        titulo : str
            Texto de la pestaña.

        Retorna
        -------
        tupla (figura, ejes, canvas)
        """
        pestana = ttk.Frame(cuaderno, style="Card.TFrame", padding=PAD_M)
        pestana.columnconfigure(0, weight=1)
        pestana.rowconfigure(0, weight=1)
        cuaderno.add(pestana, text=titulo)
        puntos_por_pulgada = round(100 * self.escala_pantalla)
        figura = Figure(figsize=(7.0, 4.0), dpi=puntos_por_pulgada)
        figura.set_facecolor(COLORS["surface"])
        ejes = figura.add_subplot(1, 1, 1)
        canvas = FigureCanvasTkAgg(figura, master=pestana)
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        barra = NavigationToolbar2Tk(canvas, pestana, pack_toolbar=False)
        barra.grid(row=1, column=0, sticky="ew")
        # La barra viene gris; le damos el fondo blanco de la tarjeta
        barra.config(background=COLORS["surface"])
        for elemento in barra.winfo_children():
            try:
                elemento.config(background=COLORS["surface"])
            except tk.TclError:
                pass
        return figura, ejes, canvas

    def _build_pestanas(self):
        """Crea las tres pestañas de gráficos."""
        self.cuaderno = ttk.Notebook(self)
        self.cuaderno.grid(row=1, column=1, sticky="nsew")
        self.figura_pulso, self.ejes_pulso, self.canvas_pulso = self.crear_pestana_con_grafico(
            self.cuaderno, "Pulso")
        self.figura_formas, self.ejes_formas, self.canvas_formas = self.crear_pestana_con_grafico(
            self.cuaderno, "Comparar formas")
        self.figura_tendencias, self.ejes_tendencias, self.canvas_tendencias = self.crear_pestana_con_grafico(
            self.cuaderno, "Tendencias")

    def _build_tabla(self):
        """Crea la tabla de mediciones, una fila por pulso."""
        tarjeta = ttk.Frame(self, style="Card.TFrame", padding=PAD_M)
        tarjeta.grid(row=2, column=1, sticky="nsew", pady=(PAD_L, 0))
        tarjeta.columnconfigure(0, weight=1)
        tarjeta.rowconfigure(0, weight=1)

        # Paso 1: la tabla con sus columnas
        identificadores = []
        for columna in COLUMNAS_DE_LA_TABLA:
            identificadores.append(columna[0])
        self.tabla = ttk.Treeview(tarjeta, columns=identificadores, show="headings", selectmode="extended")
        for columna in COLUMNAS_DE_LA_TABLA:
            self.tabla.heading(columna[0], text=columna[1])
            ancho = int(columna[2] * self.escala_pantalla)
            self.tabla.column(columna[0], width=ancho, minwidth=ancho, anchor="center", stretch=False)
        self.tabla.column("avisos", anchor="w", stretch=True)
        self.tabla.grid(row=0, column=0, sticky="nsew")

        # Paso 2: barras de desplazamiento
        barra_vertical = ttk.Scrollbar(tarjeta, orient="vertical", command=self.tabla.yview)
        barra_vertical.grid(row=0, column=1, sticky="ns")
        barra_horizontal = ttk.Scrollbar(tarjeta, orient="horizontal", command=self.tabla.xview)
        barra_horizontal.grid(row=1, column=0, sticky="ew")
        self.tabla.configure(yscrollcommand=barra_vertical.set, xscrollcommand=barra_horizontal.set)

        # Paso 3: al seleccionar filas, actualizar los gráficos
        self.tabla.bind("<<TreeviewSelect>>", self.al_cambiar_vista)

    def _build_statusbar(self):
        """Crea la barra de estado inferior con un punto de color."""
        barra = ttk.Frame(self, style="App.TFrame")
        barra.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(PAD_L, 0))
        self._dot = tk.Label(barra, text="●", bg=COLORS["bg"], fg=COLORS["muted"], font=("TkDefaultFont", 10))
        self._dot.grid(row=0, column=0, padx=(0, PAD_S))
        self._status = ttk.Label(barra, text="", style="Muted.TLabel")
        self._status.grid(row=0, column=1, sticky="w")

    def set_status(self, text, state="idle"):
        """
        Actualiza la barra de estado.

        Parámetros
        ----------
        text : str
        state : str
            "ok", "warn", "error" o "idle".
        """
        color_del_punto = COLORS["muted"]
        if state in COLORS:
            color_del_punto = COLORS[state]
        self._dot.config(fg=color_del_punto)
        self._status.config(text=text)

    # ── Carga de archivos en un hilo ───────────────────────────────────────

    def leer_opciones_de_analisis(self):
        """
        Lee la polaridad y el tramo de búsqueda del panel.

        Retorna
        -------
        tupla (polaridad, inicio_s, fin_s)

        Errores
        -------
        ValueError con un mensaje para el usuario si un tiempo está mal escrito.
        """
        inicio_s = convertir_texto_a_tiempo_s(self.texto_inicio_busqueda.get(), "Buscar entre (inicio)")
        fin_s = convertir_texto_a_tiempo_s(self.texto_fin_busqueda.get(), "Buscar entre (fin)")
        return self.polaridad.get(), inicio_s, fin_s

    def al_agregar_archivos(self):
        """Botón «Agregar archivos…»: elige .csv y los lee en un hilo."""
        if self.hay_tarea_en_curso:
            return
        rutas = filedialog.askopenfilenames(title="Agregar disparos",
                                            filetypes=[("Archivos CSV", "*.csv"), ("Todos", "*.*")])
        if len(rutas) == 0:
            return
        try:
            opciones = self.leer_opciones_de_analisis()
        except ValueError as error:
            messagebox.showerror("Dato no válido", str(error))
            return

        # No volver a cargar archivos que ya están
        rutas_ya_cargadas = []
        for archivo in self.archivos:
            rutas_ya_cargadas.append(archivo.ruta)
        rutas_nuevas = []
        for ruta in rutas:
            if ruta not in rutas_ya_cargadas:
                rutas_nuevas.append(ruta)

        self.hay_tarea_en_curso = True
        self.boton_agregar.state(["disabled"])
        self.set_status(f"Leyendo {len(rutas_nuevas)} archivos…", "warn")
        # NOTA (herramienta avanzada): usamos un hilo (threading.Thread).
        # Un hilo es una segunda línea de ejecución que corre en paralelo al
        # programa principal. Hace falta porque leer muchos archivos de 15 000
        # filas tarda varios segundos y, sin el hilo, la ventana se congelaría.
        # target es el método que corre en el hilo y args sus valores.
        hilo = threading.Thread(target=self.hilo_de_lectura, args=(rutas_nuevas, opciones), daemon=True)
        hilo.start()
        self.id_revision_pendiente = self.after(INTERVALO_REVISION_MS, self.revisar_cola)

    def hilo_de_lectura(self, rutas, opciones):
        """
        Corre en el hilo: lee y analiza cada archivo y deja el resultado en la cola.

        No toca la ventana; solo publica tuplas ("archivo", (archivo, resultados)),
        ("error", texto), ("progreso", texto) y al final ("fin", None).

        Parámetros
        ----------
        rutas : lista de str
        opciones : tupla (polaridad, inicio_s, fin_s)
        """
        for numero in range(len(rutas)):
            ruta = rutas[numero]
            self.cola_de_resultados.put(("progreso", f"Leyendo {numero + 1} de {len(rutas)}: "
                                                     f"{os.path.basename(ruta)}"))
            try:
                archivo = leer_archivo_de_disparo(ruta)
                resultados = self.analizar_archivo(archivo, opciones)
                self.cola_de_resultados.put(("archivo", (archivo, resultados)))
            except Exception as error:
                # Un archivo malo no detiene la lectura de los demás
                self.cola_de_resultados.put(("error", f"{os.path.basename(ruta)}: {error}"))
        self.cola_de_resultados.put(("fin", None))

    def analizar_archivo(self, archivo, opciones):
        """
        Analiza el pulso de cada canal de un archivo.

        Parámetros
        ----------
        archivo : ArchivoDeDisparo
        opciones : tupla (polaridad, inicio_s, fin_s)

        Retorna
        -------
        lista de ResultadoDelPulso
        """
        polaridad, inicio_s, fin_s = opciones
        resultados = []
        for canal in archivo.voltajes_por_canal:
            resultados.append(analizar_pulso(archivo, canal, polaridad, inicio_s, fin_s))
        return resultados

    def revisar_cola(self):
        """Atiende los mensajes del hilo; si sigue leyendo, vuelve a revisar en un rato."""
        errores = []
        while True:
            try:
                mensaje = self.cola_de_resultados.get_nowait()
            except queue.Empty:
                break
            tipo = mensaje[0]
            if tipo == "progreso":
                self.set_status(mensaje[1], "warn")
            elif tipo == "archivo":
                self.archivos.append(mensaje[1][0])
                for resultado in mensaje[1][1]:
                    self.resultados.append(resultado)
            elif tipo == "error":
                errores.append(mensaje[1])
            elif tipo == "fin":
                self.hay_tarea_en_curso = False

        if len(errores) > 0:
            messagebox.showerror("Archivos con problemas", "\n".join(errores))
        if self.hay_tarea_en_curso:
            self.id_revision_pendiente = self.after(INTERVALO_REVISION_MS, self.revisar_cola)
            return
        # Terminó la lectura
        self.id_revision_pendiente = None
        self.boton_agregar.state(["!disabled"])
        self.ordenar_resultados()
        self.llenar_tabla()
        self.redibujar_todo()
        self.set_status(f"{len(self.archivos)} archivos, {len(self.resultados)} pulsos analizados.", "ok")

    # ── Tabla y opciones ───────────────────────────────────────────────────

    def ordenar_resultados(self):
        """Ordena los resultados por número de disparo y luego por canal (ordenamiento por inserción)."""
        ordenados = []
        for resultado in self.resultados:
            posicion = len(ordenados)
            # Retroceder mientras el anterior deba ir después de este resultado
            while posicion > 0 and self.va_despues(ordenados[posicion - 1], resultado):
                posicion = posicion - 1
            ordenados.insert(posicion, resultado)
        self.resultados = ordenados

    def va_despues(self, resultado_a, resultado_b):
        """
        Dice si resultado_a va después de resultado_b en la tabla.

        Parámetros
        ----------
        resultado_a, resultado_b : ResultadoDelPulso

        Retorna
        -------
        bool
        """
        disparo_a = resultado_a.archivo.dato_numerico("disparo")
        disparo_b = resultado_b.archivo.dato_numerico("disparo")
        # Los disparos sin número van al final
        if disparo_a is None:
            disparo_a = float("inf")
        if disparo_b is None:
            disparo_b = float("inf")
        if disparo_a != disparo_b:
            return disparo_a > disparo_b
        return resultado_a.canal > resultado_b.canal

    def llenar_tabla(self):
        """Vuelve a escribir todas las filas de la tabla, conservando la selección."""
        seleccion_anterior = self.tabla.selection()
        for fila in self.tabla.get_children():
            self.tabla.delete(fila)
        for i in range(len(self.resultados)):
            # El identificador de cada fila es la posición del resultado en la lista
            self.tabla.insert("", "end", iid=str(i), values=valores_de_la_fila(self.resultados[i]))
        conservadas = []
        for identificador in seleccion_anterior:
            if self.tabla.exists(identificador):
                conservadas.append(identificador)
        self.tabla.selection_set(conservadas)
        self.etiqueta_archivos.config(text=f"{len(self.archivos)} archivos · {len(self.resultados)} pulsos")

    def resultados_seleccionados(self):
        """
        Retorna
        -------
        lista de ResultadoDelPulso
            Los de las filas seleccionadas, en orden.
        """
        seleccionados = []
        for identificador in self.tabla.selection():
            seleccionados.append(self.resultados[int(identificador)])
        return seleccionados

    def al_cambiar_analisis(self, evento=None):
        """
        Polaridad o tramo de búsqueda cambiados: vuelve a analizar todo lo cargado.

        Parámetros
        ----------
        evento : tk.Event o None
            Lo entrega tkinter al elegir en una lista; no se usa.
        """
        if self.hay_tarea_en_curso or len(self.archivos) == 0:
            return
        try:
            opciones = self.leer_opciones_de_analisis()
        except ValueError as error:
            messagebox.showerror("Dato no válido", str(error))
            return
        # El análisis en memoria es rápido: no necesita hilo
        self.resultados = []
        for archivo in self.archivos:
            for resultado in self.analizar_archivo(archivo, opciones):
                self.resultados.append(resultado)
        self.ordenar_resultados()
        self.llenar_tabla()
        self.redibujar_todo()
        self.set_status(f"Reanalizados {len(self.resultados)} pulsos.", "ok")

    def al_cambiar_vista(self, evento=None):
        """
        Selección u opciones de los gráficos cambiadas: redibuja.

        Parámetros
        ----------
        evento : tk.Event o None
            Lo entrega tkinter; no se usa.
        """
        self.redibujar_todo()

    def al_quitar_seleccionados(self):
        """Botón «Quitar seleccionados»: saca de la lista los archivos de las filas elegidas."""
        seleccionados = self.resultados_seleccionados()
        if len(seleccionados) == 0:
            messagebox.showinfo("Nada seleccionado", "Selecciona en la tabla las filas que quieres quitar.")
            return
        archivos_a_quitar = []
        for resultado in seleccionados:
            archivos_a_quitar.append(resultado.archivo)
        quedan_archivos = []
        for archivo in self.archivos:
            if archivo not in archivos_a_quitar:
                quedan_archivos.append(archivo)
        quedan_resultados = []
        for resultado in self.resultados:
            if resultado.archivo not in archivos_a_quitar:
                quedan_resultados.append(resultado)
        self.archivos = quedan_archivos
        self.resultados = quedan_resultados
        self.tabla.selection_set([])
        self.llenar_tabla()
        self.redibujar_todo()
        self.set_status(f"Quitados {len(archivos_a_quitar)} archivos.", "ok")

    def al_exportar(self):
        """Botón «Exportar tabla…»: guarda las mediciones en un .csv."""
        if len(self.resultados) == 0:
            messagebox.showinfo("Sin datos", "Primero agrega archivos.")
            return
        ruta = filedialog.asksaveasfilename(title="Exportar mediciones", defaultextension=".csv",
                                            initialfile="mediciones_pulsos.csv", filetypes=[("CSV", "*.csv")])
        if ruta == "":
            return
        try:
            exportar_tabla(ruta, self.resultados)
        except OSError as error:
            messagebox.showerror("No se pudo guardar", f"{error}\n\nSi el archivo está abierto, ciérralo.")
            return
        self.set_status(f"Mediciones guardadas en {os.path.basename(ruta)}", "ok")

    # ── Dibujo ─────────────────────────────────────────────────────────────

    def redibujar_todo(self):
        """Redibuja las tres pestañas y actualiza la columna de parecido."""
        # La comparación va primero porque calcula el parecido que muestran la tabla y las tendencias
        self.dibujar_comparacion()
        self.actualizar_columna_de_parecido()
        self.dibujar_pulso()
        self.dibujar_tendencias()

    def actualizar_columna_de_parecido(self):
        """Escribe en la tabla el parecido recién calculado, sin rehacer la tabla entera."""
        for i in range(len(self.resultados)):
            identificador = str(i)
            if self.tabla.exists(identificador):
                self.tabla.set(identificador, "parecido", formatear(self.resultados[i].parecido, 1.0, 3))

    def dibujar_pulso(self):
        """Pestaña «Pulso»: el pulso de la primera fila seleccionada con sus marcas."""
        ejes = self.ejes_pulso
        ejes.clear()
        estilizar_ejes(ejes)
        seleccionados = self.resultados_seleccionados()
        if len(seleccionados) == 0 and len(self.resultados) > 0:
            seleccionados = [self.resultados[0]]
        if len(seleccionados) == 0:
            escribir_mensaje_vacio(ejes, "Agrega archivos y elige una fila de la tabla")
        else:
            self.dibujar_marcas_del_pulso(ejes, seleccionados[0])
        self.figura_pulso.tight_layout()
        self.canvas_pulso.draw_idle()

    def dibujar_marcas_del_pulso(self, ejes, resultado):
        """
        Dibuja el pulso con su línea base, pico, FWHM, puntos del 10 y 90 % y el área integrada.

        Parámetros
        ----------
        ejes : matplotlib.axes.Axes
        resultado : ResultadoDelPulso
        """
        archivo = resultado.archivo
        # Tiempos en ns para el eje (división sobre todo el arreglo a la vez)
        tiempos_ns = archivo.tiempos_s / SEGUNDOS_POR_NANOSEGUNDO
        voltajes_V = archivo.voltajes_por_canal[resultado.canal]
        base_V = resultado.linea_base_V
        signo = resultado.polaridad

        # Paso 1: la señal y la línea base
        ejes.plot(tiempos_ns, voltajes_V, color=COLORS["accent"], linewidth=1.0, label=resultado.etiqueta())
        ejes.axhline(base_V, color=COLORS["muted"], linewidth=0.8, linestyle="--", label="línea base")

        # Paso 2: área integrada entre los cruces del 10 %
        if resultado.integral_Vs is not None:
            # Verdadero en los puntos entre los dos cruces (compara todo el arreglo a la vez)
            dentro = (archivo.tiempos_s >= resultado.tiempo_10_entrada_s) & (archivo.tiempos_s <= resultado.tiempo_10_salida_s)
            ejes.fill_between(tiempos_ns, base_V, voltajes_V, where=dentro, color=COLORS["accent"],
                              alpha=0.15, linewidth=0, label="área integrada")

        # Paso 3: pico y FWHM
        tiempo_pico_ns = resultado.tiempo_pico_s / SEGUNDOS_POR_NANOSEGUNDO
        ejes.plot(tiempo_pico_ns, base_V + signo * resultado.amplitud_V, marker="o", color=COLORS["text"],
                  markersize=5, linestyle="none", label="pico")
        if resultado.ancho_fwhm_s is not None:
            nivel_mitad_V = base_V + signo * FRACCION_MITAD * resultado.amplitud_V
            ejes.plot([resultado.tiempo_50_entrada_s / SEGUNDOS_POR_NANOSEGUNDO,
                       resultado.tiempo_50_salida_s / SEGUNDOS_POR_NANOSEGUNDO],
                      [nivel_mitad_V, nivel_mitad_V], color=COLORS["warn"], linewidth=2.0,
                      marker="|", markersize=10, label="FWHM")

        # Paso 4: puntos del 10 y 90 % en ambos flancos
        self.dibujar_puntos_de_cruce(ejes, resultado)

        # Paso 5: encuadre alrededor del pulso, títulos y resumen. El resumen y la
        # leyenda van en la zona libre: abajo si el pulso es negativo, arriba si es positivo.
        self.encuadrar_pulso(ejes, resultado)
        ejes.set_xlabel("tiempo desde el trigger (ns)")
        ejes.set_ylabel("voltaje (V)")
        altura_del_resumen = 0.98
        alineacion_vertical = "top"
        lugar_de_la_leyenda = "upper right"
        if signo < 0:
            altura_del_resumen = 0.02
            alineacion_vertical = "bottom"
            lugar_de_la_leyenda = "lower right"
        ejes.legend(fontsize=7, frameon=False, loc=lugar_de_la_leyenda)
        ejes.text(0.01, altura_del_resumen, self.resumen_del_pulso(resultado), transform=ejes.transAxes,
                  va=alineacion_vertical, ha="left", fontsize=8, family="monospace", color=COLORS["text"],
                  bbox={"facecolor": COLORS["surface"], "edgecolor": COLORS["border"], "alpha": 0.9})

    def dibujar_puntos_de_cruce(self, ejes, resultado):
        """
        Marca los cruces del 10 y 90 % sobre la curva.

        Parámetros
        ----------
        ejes : matplotlib.axes.Axes
        resultado : ResultadoDelPulso
        """
        cruces = [(resultado.tiempo_10_entrada_s, FRACCION_BAJA), (resultado.tiempo_90_entrada_s, FRACCION_ALTA),
                  (resultado.tiempo_90_salida_s, FRACCION_ALTA), (resultado.tiempo_10_salida_s, FRACCION_BAJA)]
        tiempos_ns = []
        niveles_V = []
        for cruce in cruces:
            if cruce[0] is None:
                continue
            tiempos_ns.append(cruce[0] / SEGUNDOS_POR_NANOSEGUNDO)
            niveles_V.append(resultado.linea_base_V + resultado.polaridad * cruce[1] * resultado.amplitud_V)
        ejes.plot(tiempos_ns, niveles_V, marker="o", markersize=4, linestyle="none",
                  color=COLORES_DE_SERIES[1], label="10 % y 90 %")

    def encuadrar_pulso(self, ejes, resultado):
        """
        Acerca la vista al pulso: unos anchos a cada lado de sus cruces del 10 %.

        Parámetros
        ----------
        ejes : matplotlib.axes.Axes
        resultado : ResultadoDelPulso
        """
        ancho_s = resultado.ancho_fwhm_s
        if ancho_s is None:
            ancho_s = 20 * SEGUNDOS_POR_NANOSEGUNDO
        inicio_s = resultado.tiempo_10_entrada_s
        fin_s = resultado.tiempo_10_salida_s
        if inicio_s is None:
            inicio_s = resultado.tiempo_pico_s
        if fin_s is None:
            fin_s = resultado.tiempo_pico_s
        margen_s = ANCHOS_A_CADA_LADO_EN_VISTA * ancho_s
        ejes.set_xlim((inicio_s - margen_s) / SEGUNDOS_POR_NANOSEGUNDO, (fin_s + margen_s) / SEGUNDOS_POR_NANOSEGUNDO)

    def resumen_del_pulso(self, resultado):
        """
        Retorna el texto con las mediciones que va en la esquina del gráfico.

        Parámetros
        ----------
        resultado : ResultadoDelPulso
        """
        archivo = resultado.archivo
        lineas = []
        lineas.append(f"Disparo {archivo.dato('disparo')}  {archivo.dato('diferencia_de_potencial_kV')} kV  "
                      f"{archivo.dato('distancia_detector_cm')} cm  {archivo.dato('gas')}")
        texto_amplitud = f"{resultado.amplitud_V:.3f} V"
        if resultado.esta_saturado:
            texto_amplitud = "≥ " + texto_amplitud + " (saturado)"
        lineas.append(f"Amplitud  {texto_amplitud}")
        lineas.append(f"FWHM      {formatear(resultado.ancho_fwhm_s, SEGUNDOS_POR_NANOSEGUNDO, 2)} ns")
        lineas.append(f"Subida    {formatear(resultado.subida_s, SEGUNDOS_POR_NANOSEGUNDO, 2)} ns   "
                      f"Bajada {formatear(resultado.bajada_s, SEGUNDOS_POR_NANOSEGUNDO, 2)} ns")
        integral_V_ns = None
        if resultado.integral_Vs is not None:
            integral_V_ns = resultado.integral_Vs / SEGUNDOS_POR_NANOSEGUNDO
        lineas.append(f"Integral  {formatear(integral_V_ns, 1.0, 2)} V·ns   "
                      f"Carga {formatear(resultado.carga_C, COULOMB_POR_NANOCOULOMB, 3)} nC")
        lineas.append(f"Base {resultado.linea_base_V * 1000:.1f} mV   Ruido {resultado.ruido_V * 1000:.1f} mV   "
                      f"S/R {resultado.senal_ruido:.0f}")
        return "\n".join(lineas)

    def resultados_a_comparar(self):
        """
        Retorna
        -------
        lista de ResultadoDelPulso
            Los seleccionados si son dos o más; si no, todos.
        """
        seleccionados = self.resultados_seleccionados()
        if len(seleccionados) >= 2:
            return seleccionados
        return self.resultados

    def dibujar_comparacion(self):
        """Pestaña «Comparar formas»: pulsos superpuestos, alineados, con su forma media."""
        ejes = self.ejes_formas
        ejes.clear()
        estilizar_ejes(ejes)
        a_comparar = self.resultados_a_comparar()
        if len(a_comparar) == 0:
            escribir_mensaje_vacio(ejes, "Agrega archivos para comparar formas")
            self.figura_formas.tight_layout()
            self.canvas_formas.draw_idle()
            return

        # Paso 1: formas en una grilla común. El parecido se borra en todos los
        # pulsos para que la tabla no muestre valores de una comparación anterior.
        for resultado in self.resultados:
            resultado.parecido = None
        normalizar = self.normalizar.get()
        grilla_s, formas, forma_media, desviacion = comparar_formas(a_comparar, self.alineacion.get(), normalizar)
        grilla_ns = grilla_s / SEGUNDOS_POR_NANOSEGUNDO

        # Paso 2: cada pulso con un color de la escala viridis (del primero al último disparo)
        escala_de_colores = colormaps["viridis"]
        for i in range(len(formas)):
            fraccion = 0.0
            if len(formas) > 1:
                fraccion = i / (len(formas) - 1)
            ejes.plot(grilla_ns, formas[i], color=escala_de_colores(0.9 * fraccion), linewidth=1.0,
                      alpha=0.85, label=a_comparar[i].etiqueta())

        # Paso 3: forma media con su banda de ± una desviación estándar
        if forma_media is not None:
            ejes.fill_between(grilla_ns, forma_media - desviacion, forma_media + desviacion,
                              color=COLORS["text"], alpha=0.12, linewidth=0, label="media ± desv.")
            ejes.plot(grilla_ns, forma_media, color=COLORS["text"], linewidth=2.0, label="forma media")

        # Paso 4: títulos
        ejes.set_xlabel(f"tiempo desde el {self.alineacion.get().lower()} (ns)")
        if normalizar:
            ejes.set_ylabel("señal / amplitud (pulso hacia arriba)")
        else:
            ejes.set_ylabel("señal sobre la línea base (V, hacia arriba)")
        # Con muchos pulsos la leyenda taparía el gráfico
        if len(formas) <= 12:
            ejes.legend(fontsize=7, frameon=False, loc="upper right")
        self.figura_formas.tight_layout()
        self.canvas_formas.draw_idle()

    def dibujar_tendencias(self):
        """Pestaña «Tendencias»: una medición contra un dato del disparo, un color por canal."""
        ejes = self.ejes_tendencias
        ejes.clear()
        estilizar_ejes(ejes)
        opcion_x = self.eje_x_tendencias.get()
        opcion_y = self.eje_y_tendencias.get()

        # Paso 1: separar los puntos por canal (cada canal es un detector)
        puntos_por_canal = {}
        for resultado in self.resultados:
            valor_x = valor_para_eje(resultado, opcion_x)
            valor_y = valor_para_eje(resultado, opcion_y)
            if valor_x is None or valor_y is None:
                continue
            if resultado.canal not in puntos_por_canal:
                puntos_por_canal[resultado.canal] = []
            puntos_por_canal[resultado.canal].append((valor_x, valor_y, resultado))

        if len(puntos_por_canal) == 0:
            escribir_mensaje_vacio(ejes, "No hay datos para estos ejes\n(revisa que los disparos tengan ese dato)")
        else:
            self.dibujar_puntos_de_tendencia(ejes, puntos_por_canal)
            ejes.set_xlabel(opcion_x)
            ejes.set_ylabel(opcion_y)
        self.figura_tendencias.tight_layout()
        self.canvas_tendencias.draw_idle()

    def dibujar_puntos_de_tendencia(self, ejes, puntos_por_canal):
        """
        Dibuja los puntos de cada canal; los saturados, huecos (su valor es una cota).

        Parámetros
        ----------
        ejes : matplotlib.axes.Axes
        puntos_por_canal : dict de str a lista de tuplas (x, y, ResultadoDelPulso)
        """
        numero_de_canal = 0
        hay_saturados = False
        for canal in puntos_por_canal:
            color = COLORES_DE_SERIES[numero_de_canal % len(COLORES_DE_SERIES)]
            numero_de_canal = numero_de_canal + 1
            for punto in puntos_por_canal[canal]:
                resultado = punto[2]
                relleno = color
                if resultado.esta_saturado:
                    relleno = "none"
                    hay_saturados = True
                ejes.plot(punto[0], punto[1], marker="o", markersize=7, linestyle="none",
                          markerfacecolor=relleno, markeredgecolor=color)
                # Número de disparo al lado de cada punto
                ejes.annotate(resultado.archivo.dato("disparo"), (punto[0], punto[1]), fontsize=7,
                              color=COLORS["muted"], xytext=(4, 4), textcoords="offset points")
            # Un punto invisible por canal, solo para la leyenda
            ejes.plot([], [], marker="o", linestyle="none", color=color, label=canal)
        if hay_saturados:
            ejes.plot([], [], marker="o", linestyle="none", markerfacecolor="none",
                      markeredgecolor=COLORS["muted"], label="saturado (cota inferior)")
        ejes.legend(fontsize=7, frameon=False, loc="best")

    # ── Cierre ─────────────────────────────────────────────────────────────

    def al_cerrar(self):
        """Cierra la ventana cancelando la revisión pendiente de la cola."""
        if self.id_revision_pendiente is not None:
            self.after_cancel(self.id_revision_pendiente)
            self.id_revision_pendiente = None
        self.master.destroy()


# ─── Nitidez y posición de la ventana (de la plantilla de interfaces) ──────

def set_dpi_awareness():
    """
    En Windows, avisa al sistema que la ventana maneja su propia escala para
    que no se vea borrosa en pantallas de alta resolución. Debe llamarse antes
    de crear tk.Tk(). Fuera de Windows no hace nada.
    """
    if sys.platform != "win32":
        return
    # ctypes permite llamar funciones de Windows desde Python
    import ctypes
    try:
        funcion_contexto = ctypes.windll.user32.SetProcessDpiAwarenessContext
        funcion_contexto.argtypes = [ctypes.c_void_p]
        funcion_contexto.restype = ctypes.c_bool
        # -4 es el código de Windows para "PER_MONITOR_AWARE_V2"
        if funcion_contexto(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def tune_scaling(root, minimo=1.0):
    """
    Ajusta la escala de tkinter a la resolución real de la pantalla.

    Parámetros
    ----------
    root : tk.Tk
    minimo : float

    Retorna
    -------
    float
        Factor respecto a 96 puntos por pulgada (el 100 % estándar).
    """
    puntos_por_pulgada = root.winfo_fpixels("1i")
    root.tk.call("tk", "scaling", puntos_por_pulgada / 72.0)
    return max(puntos_por_pulgada / 96.0, minimo)


def center(win, w, h):
    """
    Centra la ventana en la pantalla, sin que sea más grande que la pantalla.

    Parámetros
    ----------
    win : tk.Tk
    w, h : int
        Ancho y alto deseados, en píxeles.
    """
    win.update_idletasks()
    ancho_pantalla = win.winfo_screenwidth()
    alto_pantalla = win.winfo_screenheight()
    margen_px = 80
    ancho = min(w, ancho_pantalla - margen_px)
    alto = min(h, alto_pantalla - margen_px)
    x = (ancho_pantalla - ancho) // 2
    y = (alto_pantalla - alto) // 3
    win.geometry(f"{ancho}x{alto}+{x}+{y}")


def main():
    """Crea la ventana y arranca el programa."""
    set_dpi_awareness()
    raiz = tk.Tk()
    raiz.title("Análisis de pulsos")
    escala = tune_scaling(raiz)
    raiz.minsize(int(1100 * escala), int(700 * escala))
    setup_style(raiz)
    center(raiz, int(1380 * escala), int(880 * escala))
    aplicacion = App(raiz, escala)
    raiz.protocol("WM_DELETE_WINDOW", aplicacion.al_cerrar)
    raiz.mainloop()


# Programa principal. Solo se ejecuta si corremos este archivo directamente,
# no cuando otro archivo lo importa para usar sus funciones.
if __name__ == "__main__":
    main()
