content = open('new_tax/backend/.env', encoding='utf-16').read()
open('new_tax/backend/.env', 'w', encoding='utf-8').write(content)
print('Done!')