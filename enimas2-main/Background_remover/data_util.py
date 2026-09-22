from pathlib import Path
import os
import pandas as pd
from typing import Union, List, Dict, Optional, Tuple


colors_dict = {
    0: (120, 125, 135),# Default: Gray
    1: (255, 0, 0),    # Red
    2: (0, 255, 0),    # Green
    3: (0, 0, 255),    # Blue
    4: (255, 255, 0),  # Yellow
    5: (255, 165, 0),  # Orange
    6: (128, 0, 128),  # Purple
    7: (0, 255, 255),  # Cyan
    8: (255, 192, 203),# Pink
    9: (0, 0, 0),      # Black
    10: (255, 255, 255) # White
}

# search for all files with extension from folder root
def scan_folders(root_folder: str, 
                 search_extensions: list[str] | tuple[str], 
                 output_type=str, abs_paths=False) -> list:
    """
    Walk through all folders and subfolders starting from root_folder,
    and return a list of relative file paths for files matching the extensions in search_extensions.
    
    Args:
        root_folder (str): The root directory to start the search from.
        search_extensions (list): A list of file extensions to search for (e.g., ['.txt', '.jpg']).
        output_type (type): The output type for the relative file paths. Can be set to Path (default is str)
        abs_paths (bool): Return absolute file paths instead of relative paths (default is False).

    Returns:
        list: A list of relative file paths for files that have the desired extensions.
    """
    matching_files = []
    
    # Walk through all the folders and files
    for dirpath, _, filenames in os.walk(root_folder):
        for filename in filenames:
            # Get the file extension and check if it matches any in search_extensions
            if any(filename.lower().endswith(ext.lower()) for ext in search_extensions):
                # Create relative file path and append to the result list
                full_path = os.path.join(dirpath, filename)
                relative_path = os.path.relpath(full_path, root_folder)
                
                if abs_paths:
                    matching_files.append(output_type(full_path))
                else:
                    matching_files.append(output_type(relative_path))
    
    print(f"Found {len(matching_files)} files with extensions {search_extensions}")
    print(f"Root folder: {root_folder}")
    return matching_files

def get_folder_paths(root_folder):
    folder_paths = []
    for root, dirs, files in os.walk(root_folder):
        for name in dirs:
            folder_paths.append(os.path.join(root, name))
    return folder_paths

def read_from_txt(file_path: str) -> list:
    """
    Load a list of file paths from a text file.

    Args:
        file_path (str): The path to the text file.

    Returns:
        list: A list of content.
    """
    with open(file_path, 'r') as file:
        files = file.readlines()
    
    # Remove newline characters from the end of each line
    files = [file.strip() for file in files]
    
    return files

def create_content_dataframe(files: list, file_name_column: str="filename") -> pd.DataFrame:
    """
    Create a pandas DataFrame from a list of file paths.
    Dataframes contains the columns 'file', 'filename', 'filepath', 'extension'.

    file: The full file name.
    filename: The file name without extension.
    filepath: The full filepaths (as provided).
    extension: The file extension.

    Args:
        files (list): A list of file paths.
        file_name_column (str): The column name for the filename column (default is 'Filename').
    
    Returns:
        pd.DataFrame: A DataFrame with the columns 'file', 'filename', 'filepath', 'extension'.
    """
    
    # Create a DataFrame from the list of files
    df = pd.DataFrame(files, columns=["filepath"])    
    # Extract the filename and extension
    df["file"] = df["filepath"].apply(lambda x: os.path.basename(x))
    df["extension"] = df["file"].apply(lambda x: os.path.splitext(x)[1])
    df[file_name_column] = df["file"].apply(lambda x: os.path.splitext(x)[0])
    
    return df

def pathify(input: Union[str, Tuple[str], List[str]]) -> Union[Path, Tuple[Path], List[Path]]:
    """ Convert a path string or tuple or list thereof to a pathlib.Path object or tuple or list thereof.

    Args:
        input (Union[str, Tuple[str], List[str]]): Pathstring or tuple or list of pathsrings.

    Returns:
        Union[Path, Tuple[Path], List[Path]]: Path or tuple or list of Paths
    """

    # check if input is a string
    if isinstance(input, str):
        return Path(input)
    elif isinstance(input, tuple):
        return tuple([Path(item) for item in input])
    # check if input is a list
    elif isinstance(input, list):
        return [Path(item) for item in input]
    else:
        print("Input must be a string or a list of strings")
        return None

def load_xlsx_file(file_path: str):
    """
    Load an Excel file into a pandas DataFrame.
    
    Args:
        file_path (str): The path to the Excel file.
    
    Returns:
        pd.DataFrame: A pandas DataFrame containing the data from the Excel file.
    """
    # Import multi sheet excel file
    table_df = pd.read_excel(file_path)
    return table_df

def save_xlsx_file( df: pd.DataFrame, file_path: str,):
    """
    Save a pandas DataFrame to an Excel file.
    
    Args:
        df (pd.DataFrame): The DataFrame to save.
        file_path (str): The path to save the Excel file.
    """
    df.to_excel(file_path, index=False)

def load_txt_file(file_path: str) -> pd.DataFrame:
    """
    Loads a list of files and file paths from a text file.
    Convert them into da pandas dataframe.

    Args:
        file_path (str): The path to the text file.

    Returns:
        list: A list of file paths.
    """
    with open(file_path, 'r') as file:
        files = file.readlines()    
    # Remove newline characters from the end of each line
    files = [file.strip() for file in files]
    
    df = pd.DataFrame(files, columns=["filepath"])
    df["file"] = df["filepath"].apply(lambda x: os.path.basename(x))
    df["filename"] = df["file"].apply(lambda x: os.path.splitext(x)[0])
    df["extension"] = df["file"].apply(lambda x: os.path.splitext(x)[1])
    
    return df

def load_results_csv(file_path: str, separator:str = ",") -> pd.DataFrame:
    """
    Load a CSV file into a pandas DataFrame.
    
    Args:
        file_path (str): The path to the CSV file.
        separator (str): The separator used in the CSV file.
    
    Returns:
        pd.DataFrame: A pandas DataFrame containing the data from the CSV file.
    """
    # Load the CSV file into a DataFrame
    df = pd.read_csv(file_path, sep=separator)
    return df

def save_results_csv(df: pd.DataFrame, file_path: str, separator:str = ";") -> None:
    """
    Save a pandas DataFrame to a CSV file.
    
    Args:
        df (pd.DataFrame): The DataFrame to save.
        file_path (str): The path to save the CSV file.
        separator (str): The separator to use in the CSV file.
    """
    # Save the DataFrame to a CSV file
    df.to_csv(file_path, sep=separator, index=False)
