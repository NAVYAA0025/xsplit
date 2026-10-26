# X-Split: Smart Collaborative Multi-Currency Travel Expense Management System
Navyaa S, Chandana Sri Somineni, Aribam Tamanna Sharma

Flask + SQLite backend split into one module per package of our Package Model, and a single-page web UI.

```
app.py                 entry point: builds the app, error handling, security settings
modules/users.py       User Management        
modules/groups.py      Group Management       
modules/expenses.py    Expense Management     
modules/splits.py      Expense splitting      
modules/currency.py    Currency Management   
modules/ocr.py         OCR & File Handling    
modules/settlements.py Settlement Management  
modules/dashboards.py  Dashboards & Reports   
modules/guest.py       Guest participant view 
modules/db.py          Data Management        schema, migrations, connection
static/index.html      Application Interface
```
