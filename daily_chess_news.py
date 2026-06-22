#!/usr/bin/env python3
# daily_chess_news.py - Daglig automatisering av schacknyhetsinsamling

import subprocess
import logging
from datetime import datetime
import os
import json
import glob

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler('daily_chess_news.log'),
        logging.StreamHandler()
    ]
)

def run_daily_collection():
    """Kör daglig insamling av schacknyheter"""
    try:
        logging.info("🚀 Startar daglig schacknyhetsinsamling...")
        
        # Kör insamlingsprogrammet
        result = subprocess.run(
            ["python", "gambit_news_complete.py", "--collect"],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            logging.info("✅ Insamling slutförd framgångsrikt")
            
            # Kolla om det finns nya artiklar
            approval_files = glob.glob("pending_approval_*.json")
            if approval_files:
                latest_file = max(approval_files)
                with open(latest_file, 'r', encoding='utf-8') as f:
                    articles = json.load(f)
                
                if articles:
                    logging.info(f"📰 {len(articles)} nya artiklar hittades")
                    
                    # Skicka påminnelse om ohanterade artiklar
                    subprocess.run(
                        ["python", "gambit_news_complete.py", "--daily"],
                        capture_output=True,
                        text=True
                    )
                else:
                    logging.info("📭 Inga nya artiklar att rapportera")
        else:
            logging.error(f"❌ Insamling misslyckades: {result.stderr}")
            
    except Exception as e:
        logging.error(f"❌ Fel vid daglig körning: {e}")

def check_pending_articles():
    """Kontrollera om det finns väntande artiklar"""
    approval_files = glob.glob("pending_approval_*.json")
    total_pending = 0
    
    for file in approval_files:
        try:
            with open(file, 'r', encoding='utf-8') as f:
                articles = json.load(f)
                total_pending += len(articles)
        except:
            pass
    
    if total_pending > 0:
        logging.info(f"⏳ {total_pending} artiklar väntar fortfarande på godkännande")
        return True
    return False

if __name__ == "__main__":
    logging.info("="*60)
    logging.info("📅 DAGLIG SCHACKNYHETSKÖRNING")
    logging.info(f"🕐 Tid: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("="*60)
    
    # Kör insamling
    run_daily_collection()
    
    # Kolla väntande artiklar
    check_pending_articles()
    
    logging.info("="*60)
    logging.info("✅ Daglig körning avslutad")
    logging.info("="*60)