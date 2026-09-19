# Ingestiing data

- Download the dataset from 'data\source\data.txt'
- Create an EC2 instance > docker container > #######################

```
docker run -e "ACCEPT_EULA=Y" -e "MSSQL_SA_PASSWORD=FdeEnterprisePass123!" -p 1433:1433 --name legacy-mssql -d mcr.microsoft.com/mssql/server:2022-latest

```

install the requirements > pip install -r requirements.txt

echo "# supply-chain-logisitics-FDE-project" >> README.md
git init
git add README.md
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/UjjwalK08/supply-chain-logisitics-FDE-project.git
git push -u origin main