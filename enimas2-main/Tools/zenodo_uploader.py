from logging import getLogger, basicConfig, INFO, DEBUG, ERROR
from threading import Thread
from pathlib import Path
import re
import os
import hashlib
import requests
import json
import time
from github import Github
from googleapiclient.discovery import build
from oauth2client.service_account import ServiceAccountCredentials
from httplib2 import Http
from googleapiclient.http import MediaFileUpload
from PIL import Image
from datetime import datetime
import webbrowser
import sys

# Set up logging configuration
basicConfig(level=DEBUG, 
           format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = getLogger("zenodo_uploader")
logger.setLevel(DEBUG)

class Uploader(Thread):
    def __init__(self, image_path: str, sample_ID: str):
        """
        Starts the Zenodo upload of the image at the given path.

        Args:
            image_path (str): Path to the image to upload.
            sample_ID (str): Sample ID for the upload.
        """
        super().__init__()
        self.image_path = Path(image_path)
        self.sample_ID = sample_ID
        self.config = self._load_config()
        
        # Validate inputs
        if not self.image_path.exists():
            logger.error(f"Image file does not exist: {self.image_path}")
            raise FileNotFoundError(f"Image file not found: {self.image_path}")
            
        if not sample_ID or sample_ID.strip() == "":
            logger.error("Sample ID cannot be empty")
            raise ValueError("Sample ID cannot be empty")
            
    def _load_config(self):
        """Load configuration from the zenodo_config.json file."""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(current_dir, 'zenodo_config.json')
            
            if not os.path.exists(config_path):
                logger.error(f"Configuration file not found: {config_path}")
                raise FileNotFoundError(f"Configuration file not found: {config_path}")
                
            with open(config_path, 'r') as f:
                config = json.load(f)
                
            logger.info(f"Loaded configuration from {config_path}")
            return config
        except Exception as e:
            logger.error(f"Error loading configuration: {str(e)}", exc_info=True)
            raise

    def run(self):
        """Run the Zenodo upload process."""
        try:
            logger.info(f"Starting Zenodo upload for {self.image_path} with sample ID {self.sample_ID}")
            self.zenodo_upload(self.image_path, self.sample_ID)
        except Exception as e:
            logger.error(f"Error during Zenodo upload: {str(e)}", exc_info=True)

    def zenodo_upload(self, image_path: Path, sampleid: str):
        try:
            basefilename = os.path.basename(str(image_path))
            logger.info(f"Processing file: {basefilename}")

            scopes = 'https://www.googleapis.com/auth/drive'
            
            # Get the credentials path from config
            credentials_path = self.config['google_drive']['credentials_path']
            if not os.path.isabs(credentials_path):
                current_dir = os.path.dirname(os.path.abspath(__file__))
                credentials_path = os.path.join(current_dir, os.path.basename(credentials_path))
            
            try:
                # Create credentials from the service account file
                credentials = ServiceAccountCredentials.from_json_keyfile_name(
                    filename=credentials_path, 
                    scopes=scopes
                )
                http_auth = credentials.authorize(Http())
                drive = build('drive', 'v3', http=http_auth)
            except Exception as e:
                logger.error(f"Failed to authenticate with Google Drive: {str(e)}", exc_info=True)
                raise

            # uploads data to google drive
            try:
                logger.info("Uploading to Google Drive...")
                uploaded_link = self.run_googleapi(str(image_path), basefilename, drive)
                logger.info(f"File uploaded to Google Drive: {uploaded_link}")
            except Exception as e:
                logger.error(f"Failed to upload to Google Drive: {str(e)}", exc_info=True)
                raise

            # generates md5hash
            try:
                logger.info("Generating MD5 hash...")
                md5hash = self.md5(str(image_path))
                logger.info(f"MD5 hash: {md5hash}")
            except Exception as e:
                logger.error(f"Failed to generate MD5 hash: {str(e)}", exc_info=True)
                raise

            # gets image dimensions
            try:
                logger.info("Getting image dimensions...")
                width, height = self.get_num_pixels(str(image_path))
                logger.info(f"Image dimensions: {width}x{height}")
            except Exception as e:
                logger.error(f"Failed to get image dimensions: {str(e)}", exc_info=True)
                raise

            # connects to github
            try:
                logger.info("Connecting to GitHub...")
                g, repo = self.connect_to_github()
                logger.info("Connected to GitHub repository")
            except Exception as e:
                logger.error(f"Failed to connect to GitHub: {str(e)}", exc_info=True)
                raise

            # obtains metadata from github event.txt file using sampleid
            try:
                logger.info(f"Reading event file for sample ID: {sampleid}")
                sampleID, sample_metadata = self.read_eventfile(repo, sampleid)
                logger.info(f"Found metadata for sample ID: {sampleID}")
            except Exception as e:
                logger.error(f"Failed to read event file: {str(e)}", exc_info=True)
                raise

            # creates new entries for github commit
            try:
                logger.info("Creating GitHub commit entries...")
                current_year = datetime.now().year
                occurrenceline = self.create_occurrenceline(sampleID, sample_metadata, md5hash, uploaded_link, basefilename)
                multimedialine = self.create_multimedialine(basefilename, sampleID, sample_metadata, md5hash, current_year, uploaded_link, width, height)
                logger.info("Created GitHub commit entries")
            except Exception as e:
                logger.error(f"Failed to create GitHub commit entries: {str(e)}", exc_info=True)
                raise

            # commits to github
            try:
                logger.info("Updating GitHub repository...")
                self.run_update_github(occurrenceline, multimedialine, repo, sampleid, basefilename)
                logger.info("Updated GitHub repository")
            except Exception as e:
                logger.error(f"Failed to update GitHub repository: {str(e)}", exc_info=True)
                raise

            # Prepare Zenodo URLs
            base_url = self.config['zenodo']['base_url']
            physurl = f"{base_url}/api/records?all_versions=false&q=alternate.identifier:%22urn%3Alsid%3AMfN%3AEnto%3A{basefilename.split('_')[1]}%22&f=resource_type%3Aphysicalobject"
            photourl = f"{base_url}/api/records?all_versions=false&q=alternate.identifier%3A%22hash%3A%2F%2Fmd5%2F{md5hash}%22&f=resource_type%3Aimage"

            # Runs a timer and keeps querying the weblinks to get the links.
            logger.info("Checking Zenodo URLs...")
            physicalobjectweblink, photoweblink = False, False
            runtime = 0
            max_runtime = self.config['zenodo']['timeout_seconds']  # Max timeout from config
            check_interval = self.config['zenodo']['check_interval_seconds']  # Check interval from config
            
            while runtime < max_runtime:
                try:
                    physicalobjectweblink, photoweblink = self.checkurl(physurl, photourl)
                    
                    if physicalobjectweblink and photoweblink:
                        logger.info(f"Found both URLs - Physical: {physicalobjectweblink}, Photo: {photoweblink}")
                        break
                    
                    if not physicalobjectweblink:
                        logger.debug("Still searching for physical object weblink...")
                    else:
                        logger.info(f"Found physical object weblink: {physicalobjectweblink}")
                        
                    if not photoweblink:
                        logger.debug("Still searching for photo weblink...")
                    else:
                        logger.info(f"Found photo weblink: {photoweblink}")
                        
                    time.sleep(check_interval)
                    runtime += check_interval
                    
                    # Log progress every minute
                    if runtime % 60 == 0:
                        logger.info(f"Still waiting for Zenodo URLs after {runtime//60} minutes...")
                        
                except Exception as e:
                    logger.error(f"Error checking Zenodo URLs: {str(e)}")
                    time.sleep(check_interval)
                    runtime += check_interval
            
            if runtime >= max_runtime:
                logger.warning("Timed out waiting for Zenodo URLs")
            
            # Open browser tabs if URLs were found
            if photoweblink:
                logger.info(f"Opening photo weblink: {photoweblink}")
                webbrowser.open_new_tab(photoweblink)
            
            if physicalobjectweblink:
                logger.info(f"Opening physical object weblink: {physicalobjectweblink}")
                webbrowser.open_new_tab(physicalobjectweblink)
                
            logger.info("Zenodo upload process completed")
            
        except Exception as e:
            logger.error(f"Error in zenodo_upload: {str(e)}", exc_info=True)
            raise

    def run_googleapi(self, inpath, filename, drive):
        try:
            logger.debug(f"Uploading file to Google Drive: {inpath}")
            
            # Get folder ID from configuration
            folder_id = self.config['google_drive']['folder_id']
            
            file_metadata = {'name': filename, 'mimeType': '*/*', 'parents': folder_id}
            media = MediaFileUpload(inpath, mimetype='*/*', resumable=True)
            file = drive.files().create(body=file_metadata, media_body=media, fields='id').execute()
            ID = file.get('id')
            
            logger.debug(f"File uploaded with ID: {ID}, setting permissions")
            permissions = {'type': 'anyone', 'role': 'writer'}
            drive.permissions().create(fileId=ID, body=permissions).execute()
            
            request = drive.files().get(fileId=ID, supportsAllDrives=True, fields='webContentLink').execute()
            return request['webContentLink']
        except Exception as e:
            logger.error(f"Error in run_googleapi: {str(e)}", exc_info=True)
            raise

    def md5(self, file_path):
        try:
            logger.debug(f"Calculating MD5 hash for: {file_path}")
            with open(file_path, 'rb') as f:
                file_hash = hashlib.md5()
                while chunk := f.read(8192):
                    file_hash.update(chunk)
            return file_hash.hexdigest()
        except Exception as e:
            logger.error(f"Error calculating MD5 hash: {str(e)}", exc_info=True)
            raise

    def get_num_pixels(self, filepath):
        try:
            logger.debug(f"Getting image dimensions for: {filepath}")
            with Image.open(filepath) as img:
                width, height = img.size
            return width, height
        except Exception as e:
            logger.error(f"Error getting image dimensions: {str(e)}", exc_info=True)
            raise

    def connect_to_github(self):
        try:
            # Get GitHub token from the environment, falling back to configuration
            token = os.environ.get('GITHUB_TOKEN') or self.config['github'].get('token')
            if not token:
                logger.error("No GitHub token configured")
                raise ValueError(
                    "No GitHub token found. Set the GITHUB_TOKEN environment variable "
                    "or fill in 'github.token' in zenodo_config.json."
                )
            repository = self.config['github']['repository']
            
            logger.debug("Connecting to GitHub")
            
            g = Github(token)
            gh_user = g.get_user()
            login = gh_user.login
            logger.debug(f"Logged in as: {login}")
            
            repo = g.get_repo(repository)
            logger.debug(f"Connected to repository: {repository}")
            
            return g, repo
        except Exception as e:
            logger.error(f"Error connecting to GitHub: {str(e)}", exc_info=True)
            raise

    def read_eventfile(self, githubinstance, sampleID):
        try:
            logger.debug(f"Reading event file for sample ID: {sampleID}")
            data = githubinstance.get_contents(path="event.txt")
            content = data.decoded_content
            content = content.decode("utf-8").split('\n')
            
            eventdict = {}
            header = content[0].split('\t')
            
            for event in content[1:]:
                m = event.split('\t')
                if len(m) == 16:
                    eventdict[m[1]] = {}
                    for i, j in enumerate(header):
                        eventdict[m[1]][j] = m[i]
            
            full_sample_id = "urn:lsid:MfN:Ento:" + sampleID
            
            if full_sample_id not in eventdict:
                logger.error(f"Sample ID {full_sample_id} not found in event file")
                raise ValueError(f"Sample ID {full_sample_id} not found in event file")
                
            return full_sample_id, eventdict[full_sample_id]
        except Exception as e:
            logger.error(f"Error reading event file: {str(e)}", exc_info=True)
            raise

    def create_occurrenceline(self, sampleID, sample_metadata, md5hash, uploaded_link, basefilename):
        try:
            logger.debug(f"Creating occurrence line for sample ID: {sampleID}")
            specimencode = basefilename.split('_')[1]
            dynamicproperties = f'{{"keyImageFilename":"{basefilename}","keyImageID":"hash://md5/{md5hash}","keyImageUrl":"{uploaded_link}"}}'

            occurrenceline = "\t".join([
                sampleID, "Event", "urn:lsid:MfN:Ento:" + specimencode, "PreservedSpecimen",
                sampleID.split(":")[2], sampleID.split(":")[3], specimencode, "", "",
                sampleID, sample_metadata["eventDate"], sample_metadata["countryCode"],
                "Insecta", "", dynamicproperties
            ])
            
            return occurrenceline
        except Exception as e:
            logger.error(f"Error creating occurrence line: {str(e)}", exc_info=True)
            raise

    def create_multimedialine(self, basefilename, sampleID, sample_metadata, md5hash, current_year, uploaded_link, width, height):
        try:
            logger.debug(f"Creating multimedia line for file: {basefilename}")
            specimencode = basefilename.split('_')[1]
            
            # Get Zenodo base URL for search links, removing '/api' if present
            zenodo_search_base = self.config['zenodo']['base_url'].replace('/api', '')
            # If it's a sandbox URL, use the main zenodo.org domain for the search
            if 'sandbox' in zenodo_search_base:
                zenodo_search_base = "https://zenodo.org"
                
            multimedialine = "\t".join([
                sampleID, basefilename, "StillImage", "Photo", "Photo of specimen " + specimencode,
                sample_metadata["eventDate"], "eng", "",
                f"{zenodo_search_base}/search?q=_files.checksum%3A%22md5%3A{md5hash}%22&f=allversions%3Atrue",
                "(c) Museum für Naturkunde Berlin – CC0", "Museum für Naturkunde Berlin",
                "https://www.museumfuernaturkunde.berlin/", "", "", "", "",
                f"Photo of Specimen {specimencode}. Uploaded by Plazi for the Museum für Naturkunde Berlin.",
                "MfN | DarkTaxon", "", "", str(current_year), "", "", "whole specimen", "", "", "",
                "Entomoscope", "Focal stacking", "", "", f"urn:lsid:MfN:Ento:{specimencode}", "",
                uploaded_link, "tiff", "mediumQualityFurtherInformationURL", "MD5", md5hash,
                str(width), str(height)
            ])
            
            return multimedialine
        except Exception as e:
            logger.error(f"Error creating multimedia line: {str(e)}", exc_info=True)
            raise

    def run_update_github(self, occurrenceline, multimedialine, githubinstance, sampleID, basefilename):
        try:
            logger.debug(f"Updating GitHub repository with new data for file: {basefilename}")
            
            # Update multimedia.txt
            data = githubinstance.get_contents(path="multimedia.txt")
            multimedia_result = githubinstance.update_file(
                "multimedia.txt",
                f"multimedia {basefilename}",
                data.decoded_content.decode() + "\n" + multimedialine,
                sha=data.sha
            )
            logger.debug(f"Updated multimedia.txt: {multimedia_result}")
            
            # Update occurrence.txt
            data = githubinstance.get_contents(path="occurrence.txt")
            occurrence_result = githubinstance.update_file(
                "occurrence.txt",
                f"occurrence {basefilename}",
                data.decoded_content.decode() + "\n" + occurrenceline,
                sha=data.sha
            )
            logger.debug(f"Updated occurrence.txt: {occurrence_result}")
            
            return multimedia_result, occurrence_result
        except Exception as e:
            logger.error(f"Error updating GitHub repository: {str(e)}", exc_info=True)
            raise

    def checkurl(self, url1, url2):
        """
        Checks two Zenodo API URLs to find records for a physical object and a photo.
        
        Args:
            url1 (str): API URL to check for physical object record
            url2 (str): API URL to check for photo record
            
        Returns:
            tuple: (physical_object_link, photo_link) where each can be a URL string or False if not found
            
        This method polls the Zenodo API to find the links to the published records.
        The first URL searches for physical object records, the second for image records.
        """
        try:
            logger.debug(f"Checking URLs: {url1} and {url2}")
            
            # Get timeout from config
            timeout = self.config['zenodo'].get('request_timeout', 10)
            
            try:
                logger.debug(f"Requesting URL1: {url1}")
                request1 = requests.get(url1, timeout=timeout)
                request1.raise_for_status()
                j = json.loads(request1.text)
            except requests.RequestException as e:
                logger.error(f"Error requesting URL1 {url1}: {str(e)}")
                return False, False
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing JSON from URL1 {url1}: {str(e)}")
                return False, False
            
            if j['hits']['total'] == 0:
                try:
                    logger.debug(f"Requesting URL2: {url2}")
                    request2 = requests.get(url2, timeout=timeout)
                    request2.raise_for_status()
                    j2 = json.loads(request2.text)
                except requests.RequestException as e:
                    logger.error(f"Error requesting URL2 {url2}: {str(e)}")
                    return False, False
                except json.JSONDecodeError as e:
                    logger.error(f"Error parsing JSON from URL2 {url2}: {str(e)}")
                    return False, False
                
                if j2['hits']['total'] >= 1:
                    for i, x in enumerate(j2['hits']['hits']):
                        if j2['hits']['hits'][i]['metadata']['resource_type']['type'] == 'image':
                            photoweblink = j2['hits']['hits'][i]['links']['self_html']
                            return False, photoweblink
                else:
                    return False, False
            elif j['hits']['total'] >= 1:
                physicalobjectweblink = ''
                for i, x in enumerate(j['hits']['hits']):
                    if j['hits']['hits'][i]['metadata']['resource_type']['type'] == 'physicalobject':
                        physicalobjectweblink = j['hits']['hits'][i]['links']['self_html']
                
                if physicalobjectweblink != '':
                    try:
                        logger.debug(f"Requesting URL2: {url2}")
                        request2 = requests.get(url2, timeout=timeout)
                        request2.raise_for_status()
                        j2 = json.loads(request2.text)
                    except requests.RequestException as e:
                        logger.error(f"Error requesting URL2 {url2}: {str(e)}")
                        return physicalobjectweblink, False
                    except json.JSONDecodeError as e:
                        logger.error(f"Error parsing JSON from URL2 {url2}: {str(e)}")
                        return physicalobjectweblink, False
                    
                    if j2['hits']['total'] >= 1:
                        for i, x in enumerate(j2['hits']['hits']):
                            if j2['hits']['hits'][i]['metadata']['resource_type']['type'] == 'image':
                                photoweblink = j2['hits']['hits'][i]['links']['self_html']
                                return physicalobjectweblink, photoweblink
                    else:
                        return physicalobjectweblink, False
                else:
                    try:
                        logger.debug(f"Requesting URL2: {url2}")
                        request2 = requests.get(url2, timeout=timeout)
                        request2.raise_for_status()
                        j2 = json.loads(request2.text)
                    except requests.RequestException as e:
                        logger.error(f"Error requesting URL2 {url2}: {str(e)}")
                        return False, False
                    except json.JSONDecodeError as e:
                        logger.error(f"Error parsing JSON from URL2 {url2}: {str(e)}")
                        return False, False
                    
                    photoweblink = None
                    if j2['hits']['total'] >= 1:
                        for i, x in enumerate(j2['hits']['hits']):
                            if j2['hits']['hits'][i]['metadata']['resource_type']['type'] == 'image':
                                photoweblink = j2['hits']['hits'][i]['links']['self_html']
                                break
                        return False, photoweblink if photoweblink else False
                    else:
                        return False, False
            
            return False, False
        except Exception as e:
            logger.error(f"Error checking URLs: {str(e)}", exc_info=True)
            return False, False
