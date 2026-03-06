# CNC Transfer - Dosya Aktarim Sistemi

FOCAS protokolu ile Fanuc CNC makinelere dosya transferi yapan masaustu uygulamasi.

## Ozellikler

- **FOCAS CNC Bellek** - DLL uzerinden direkt CNC bellege program yukleme (Torna)
- **FOCAS CF_TEXT** - Raw TCP ile CF karta dosya yazma (Freze)
- **FTP** - Standart FTP protokolu destegi
- **Program Sil + Gonder** - O numarali programi silip yerine yenisini yukle
- **CNC Yedekleme** - Makinedeki programlari bilgisayara yedekle
- **Otomatik Durum Kontrolu** - Makine baglanti durumunu otomatik izle
- **Modern Arayuz** - Chrome/Material Design tarzi CustomTkinter GUI

## Kurulum

1. `CNC_Transfer_Setup.zip` dosyasini indirin ve acin
2. `setup.bat` dosyasini cift tiklayarak calistirin
3. Kurulum otomatik olarak:
   - Python yukler (yoksa)
   - Gerekli paketleri kurar (customtkinter, chattertools)
   - Uygulama dosyalarini kopyalar
   - Masaustune kisayol olusturur

## Manuel Kurulum

```bash
pip install customtkinter chattertools
python dosya_aktarim.py
```

## Makineler

| Makine | IP | Port | Protokol |
|--------|----|------|----------|
| Torna | 192.168.1.22 | 8193 | FOCAS CNC Bellek |
| Freze | 192.168.1.49 | 8193 | FOCAS CF_TEXT |

## Gereksinimler

- Windows 10/11
- Python 3.10+
- CustomTkinter
- chattertools (FOCAS DLL icin)
