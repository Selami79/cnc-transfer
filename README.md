# CNC Transfer - Dosya Aktarim Sistemi

FOCAS protokolu ile Fanuc CNC makinelere dosya transferi yapan masaustu uygulamasi.

## Ozellikler

- **FOCAS CNC Bellek** - DLL uzerinden direkt CNC bellege program yukleme (Torna)
- **FOCAS CF_TEXT** - Raw TCP ile CF karta dosya yazma (Freze)
- **CF Dosyalar (Listele + Sil)** - CF_TEXT karttaki programlari listeleme ve tek tek silme (Freze). Silme protokolu FANUC Program Transfer Tool ile aynidir.
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

## CF_TEXT: Listing and Deleting Files

On a `FOCAS CF_TEXT` machine card, click **CF Files** to open the CF card file manager.
It lists the programs on the CF card; click **Delete** next to a file to remove it (a confirmation is asked first).

Notes:
- The CF card must be the selected device on the CNC panel.
- A program that is selected/running on the CNC cannot be deleted (CNC returns code 5).
- Verified on Series 0i-MF. Other controls may behave differently.
