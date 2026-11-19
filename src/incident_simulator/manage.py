import argparse
import getpass
import json
import secrets
from pathlib import Path
from alembic import command
from alembic.config import Config
from .auth import create_user

def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('migrate')
    user=sub.add_parser('create-user')
    user.add_argument('--username',required=True)
    user.add_argument('--role',choices=['admin','operator','viewer'],default='operator')
    user.add_argument('--credentials-file',type=Path)
    args=parser.parse_args()
    if args.action=='migrate': command.upgrade(Config('alembic.ini'),'head')
    else:
        password=secrets.token_urlsafe(24) if args.credentials_file else getpass.getpass('Password (12+ characters): ')
        if args.credentials_file and args.credentials_file.exists(): raise SystemExit('Credential file already exists')
        created=create_user(args.username,password,args.role)
        if args.credentials_file:
            args.credentials_file.parent.mkdir(parents=True,exist_ok=True)
            with args.credentials_file.open('x',encoding='utf-8') as file: json.dump({'username':created.username,'password':password},file)
            print('Local credentials saved. Keep private and delete after recording the password.')
        print('User created: '+created.username)

if __name__=='__main__': main()
