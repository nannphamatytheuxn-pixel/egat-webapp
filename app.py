import streamlit as st
import ee
import geemap.foliumap as geemap
import geopandas as gpd
import pandas as pd
from datetime import datetime, timedelta
import json
import zipfile
import os
import tempfile
from streamlit_folium import st_folium

# 1. ตั้งค่าหน้าเว็บ
st.set_page_config(page_title="EGAT Encroachment", layout="wide")
st.title("🛰️ ระบบตรวจสอบการลุกล้ำพื้นที่ กฟผ. (อัตโนมัติ)")

# 2. ระบบล็อกอินอัตโนมัติเบื้องหลัง
@st.cache_resource
def init_ee():
    try:
        with open('credentials.json') as f:
            credentials_dict = json.load(f)
        credentials = ee.ServiceAccountCredentials(credentials_dict['client_email'], key_data=json.dumps(credentials_dict))
        ee.Initialize(credentials, project='egat-encroachment')
        return True
    except Exception as e:
        st.error(f"ไม่สามารถเชื่อมต่อฐานข้อมูลได้: {e}")
        return False

if init_ee():
    st.sidebar.success("✅ เชื่อมต่อ Google Earth Engine สำเร็จ")
    
    # 3. เลือกวิธีนำเข้าพื้นที่ (เมนูด้านซ้าย)
    st.sidebar.header("📍 1. กำหนดขอบเขตพื้นที่")
    input_method = st.sidebar.radio("เลือกวิธี:", ["อัปโหลดไฟล์ Shapefile (.zip)", "วาดพื้นที่บนแผนที่"])
    
    egat_aoi = None # ตัวแปรเก็บขอบเขตพื้นที่
    
    # กรณีที่ 1: อัปโหลดไฟล์
    if input_method == "อัปโหลดไฟล์ Shapefile (.zip)":
        uploaded_zip = st.sidebar.file_uploader("อัปโหลดไฟล์ Shapefile (.zip)", type="zip")
        if uploaded_zip is not None:
            with tempfile.TemporaryDirectory() as tmp_dir:
                with zipfile.ZipFile(uploaded_zip, 'r') as zip_ref:
                    zip_ref.extractall(tmp_dir)
                shp_files = [f for f in os.listdir(tmp_dir) if f.endswith('.shp')]
                
                if shp_files:
                    shp_path = os.path.join(tmp_dir, shp_files[0])
                    gdf = gpd.read_file(shp_path)
                    if gdf.crs != "EPSG:4326":
                        gdf = gdf.to_crs(epsg=4326)
                    egat_aoi = geemap.geojson_to_ee(gdf.__geo_interface__)
                    st.sidebar.success("✅ อ่านไฟล์ขอบเขตพื้นที่สำเร็จ")
                else:
                    st.sidebar.error("❌ ไม่พบไฟล์ .shp ใน Zip ครับ")

    # กรณีที่ 2: วาดบนแผนที่
    else:
        st.markdown("### 🖌️ วาดพื้นที่ที่ต้องการตรวจสอบ")
        st.markdown("ใช้เครื่องมือ **รูปห้าเหลี่ยม** (Draw a polygon) ที่แถบซ้ายของแผนที่ วาดคลุมพื้นที่ที่ต้องการตรวจสอบ แล้วเลื่อนลงไปกดปุ่มเริ่มวิเคราะห์")
        
        draw_map = geemap.Map(center=[13.8, 100.6], zoom=6)
        draw_map.add_basemap('HYBRID')
        
        map_output = st_folium(draw_map, width=1000, height=500)
        
        if map_output and map_output.get("last_active_drawing"):
            geom = map_output["last_active_drawing"]["geometry"]
            egat_aoi = ee.Geometry(geom)
            st.success("✅ รับค่าพื้นที่ที่คุณวาดเรียบร้อยแล้ว!")

    # 4. ปุ่มเริ่มวิเคราะห์
    if egat_aoi is not None:
        st.sidebar.header("🚀 2. ประมวลผล")
        if st.sidebar.button("เริ่มวิเคราะห์ข้อมูลดาวเทียม", type="primary"):
            
            with st.spinner("กำลังดึงภาพดาวเทียมย้อนหลัง 10 ปี และเปรียบเทียบข้อมูล... (อาจใช้เวลา 1-2 นาที)"):
                today = datetime.now()
                past_1y = today - timedelta(days=365)
                past_10y = today - timedelta(days=365 * 10)
                
                p10_start, p10_end = (past_10y - timedelta(days=90)).strftime('%Y-%m-%d'), (past_10y + timedelta(days=90)).strftime('%Y-%m-%d')
                p1_start, p1_end = (past_1y - timedelta(days=90)).strftime('%Y-%m-%d'), (past_1y + timedelta(days=90)).strftime('%Y-%m-%d')
                
                def process_image(image):
                    qa = image.select('QA60')
                    mask = qa.bitwiseAnd(1<<10).eq(0).And(qa.bitwiseAnd(1<<11).eq(0))
                    img = image.updateMask(mask).divide(10000)
                    
                    ndvi = img.normalizedDifference(['B8', 'B4']).rename('NDVI')
                    ndbi = img.normalizedDifference(['B11', 'B8']).rename('NDBI')
                    ndwi = img.normalizedDifference(['B3', 'B8']).rename('NDWI')
                    bare_soil = img.normalizedDifference(['B11', 'B4']).rename('BareSoil')
                    return img.addBands([ndvi, ndbi, ndwi, bare_soil])

                collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
                    .filterBounds(egat_aoi).filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20)).map(process_image)
                
                img_old = collection.filterDate(p10_start, p10_end).median().clip(egat_aoi)
                img_new = collection.filterDate(p1_start, p1_end).median().clip(egat_aoi)
                
                d_ndbi = img_new.select('NDBI').subtract(img_old.select('NDBI'))
                d_ndvi = img_old.select('NDVI').subtract(img_new.select('NDVI'))
                
                encroach = d_ndbi.gt(0.05).And(d_ndvi.gt(0.03)) \
                    .And(img_new.select('NDWI').lt(0.15)) \
                    .And(img_new.select('BareSoil').lt(0.20))
                
                built_up = encroach.And(d_ndbi.gt(0.12))
                agri = encroach.And(d_ndbi.lte(0.12))
                
                st.markdown("---")
                st.markdown(f"### 📊 ผลการวิเคราะห์เปรียบเทียบ: ปี {past_10y.year} VS ปี {past_1y.year}")
                
                result_map = geemap.Map()
                result_map.centerObject(egat_aoi, 14)
                rgb_vis = {'min': 0.0, 'max': 0.3, 'bands': ['B4', 'B3', 'B2']}
                
                result_map.addLayer(img_old, rgb_vis, 'ภาพ 10 ปีก่อน', False)
                result_map.addLayer(img_new, rgb_vis, 'ภาพ 1 ปีก่อน')
                result_map.addLayer(built_up.updateMask(built_up), {'palette': ['red']}, '⚠️ ลุกล้ำ: สิ่งปลูกสร้าง (สีแดง)')
                result_map.addLayer(agri.updateMask(agri), {'palette': ['yellow']}, '⚠️ ลุกล้ำ: เกษตรกรรม (สีเหลือง)')
                
                result_map.to_streamlit(height=600)
                st.success("✅ วิเคราะห์เสร็จสิ้น! สามารถเปิด/ปิดเลเยอร์ที่ไอคอนมุมขวาบนของแผนที่เพื่อเปรียบเทียบได้เลยครับ")